# EchoTrace

EchoTrace 是一款具有长期记忆能力的个人树洞应用。用户可以直接记录文字，或与 Companion 连续对话；每条原始 Moment 持续保存并可检索，按周形成带来源的简短摘要，再基于真实历史生成可回溯的洞察。

当前版本为可运行的 V1，仅保留文字输入。语音转写、图片和文件输入计划在后续版本评估。

## 核心能力

- 「留一下 / 聊一会儿」双入口
- Moment、Thread 与历史对话恢复
- Companion 短期对话记忆
- 原始 Moment 分块索引、每周证据摘要与历史回顾；旧版 Memory Curator 仅保留兼容读取
- Personal Memory RAG 按需从原文、历史 Memory 和摘要线索召回原始证据
- `Orchestrator → Temporal / Pattern → Verifier → Synthesizer` 洞察链路
- 支持证据、反证和原始 Moment 回溯
- Supabase Auth、PostgreSQL RLS 与 API 双层 `user_id` 隔离
- 每周一次条件触发、版本缓存与数据库埋点；空白周不调用模型

## 技术栈

- 客户端：Expo、React Native、TypeScript、Expo Router
- API：FastAPI、Python 3.12
- 数据：Supabase Auth、PostgreSQL、pgvector、pg_trgm、RLS
- 模型：阿里云百炼 OpenAI-compatible API，模型名称通过环境变量配置
- 部署配置：Vercel Web + API，EAS Android Preview

本地质量版已切换到“原文持续保存 + 周期摘要 + 每周洞察”。当前 Supabase 已应用新迁移，本地真实冒烟已覆盖后台原文索引、长期召回、Companion、周摘要、Evidence 和完整 Multi-Agent Insight；当前线上 Demo 仍是此前版本，尚未部署本次变更。每周整理在本地打开“AI 发现”时检查并后台运行，不占用前台等待；线上后台 Worker 是后续部署工作。

## 项目结构

```text
apps/mobile       Expo React Native 客户端
apps/api          FastAPI、Memory RAG 与 Multi-Agent Insight
supabase          数据库迁移、RLS 和检索函数
evaluation        可复现的模型、检索与 Bad Case 评测代码
```

## 本地运行

### API

```powershell
cd apps/api
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

### 客户端

```powershell
cd apps/mobile
Copy-Item .env.example .env
npm install
npx expo start
```

真实 `.env` 不进入版本库。前端只使用 Supabase publishable key；百炼 API Key 仅配置在 API 服务端。

新周报数据库结构见 [202609230001_evidence_preserving_weekly_memory.sql](supabase/migrations/202609230001_evidence_preserving_weekly_memory.sql)。该迁移已应用于当前 Supabase 项目。

## 自动化与模型评测

当前本地基线：

- 当前本地 Python/API/安全测试：31/31 通过（仅工程测试，不是效果指标）
- Expo Doctor：21/21 通过
- 历史组件级 Memory Curator：24/24 受控 Case 通过
- 历史组件级 Embedding Retrieval：15/15，Recall@3=1.0，MRR=0.9333
- 历史组件级 Insight / Bad Case：12/12，Grounded Claim Rate=1.0

上述 51 条结果是真实模型调用，但只代表早期组件级基线，不等同于完整产品 Workflow 的正式效果。

正式 Evaluation 为 36 个产品场景，其中旧 Curator 的 9 个场景已改为原文长期召回与过度推断检查。支持重试、超时、断点、成功缓存、单 Case/分类筛选、LLM Judge 和人工 Review 分流。新架构已完成 3 个效果 Case 与 1 个周报脚本的工程冒烟；正式全量 Evaluation 尚未执行，不应把这些冒烟或旧组件分数写成正式指标。

运行方式：

```powershell
cd apps/api
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app tests ..\..\evaluation
.\.venv\Scripts\python.exe ..\..\evaluation\run_evaluation.py --suite all
```

正式产品效果评测在项目根目录运行：

```powershell
.\apps\api\.venv\Scripts\python.exe evaluation\run_eval.py
```

详细配置、筛选方式和输出说明见 `evaluation/README.md`。

只验证本地后台索引与每周回顾：

```powershell
.\apps\api\.venv\Scripts\python.exe evaluation\run_weekly_smoke.py
```

### 公网黑盒验收

公网验收使用独立测试账号真实调用已部署的 Supabase、Vercel API 和百炼模型，不依赖浏览器登录态。
复制 `evaluation/live-e2e.example.env` 为 `evaluation/live-e2e.env`，只填写已确认邮箱的专用测试账号；该文件已被 Git 忽略。

```powershell
cd apps/api
.\.venv\Scripts\python.exe ..\..\evaluation\run_live_e2e.py --suite smoke
.\.venv\Scripts\python.exe ..\..\evaluation\run_live_e2e.py --suite full
```

- `smoke`：验证公网健康检查、真实登录、Moment 写入与历史读取，不调用模型。
- `full`：继续验证 Memory Curator、Embedding、短期对话、长期召回、Insight、Evidence 与证据不足拒答。
- 每次运行会生成本地 JSON 报告；报告和测试账号凭据都不会进入 Git。

同一账号也可用于公网网页自动验收。测试默认调用本机已安装的 Microsoft Edge，无需下载额外浏览器：

```powershell
cd apps/mobile
npm run test:e2e:web
```

## 安全边界

- 关于用户过去经历、偏好、状态和变化的陈述必须关联当前用户的真实 Moment。
- Evidence 不属于当前用户、证据不足或结构化校验失败时，结果不会写入正式 Memory/Insight。
- Moment 正文不进入普通运行日志；模型调用仅记录模型、版本、延迟、Token 和证据数量等元数据。
