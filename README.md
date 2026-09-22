# EchoTrace

EchoTrace 是一款具有长期记忆能力的个人树洞应用。用户可以直接记录文字，或与 Companion 连续对话；系统会把值得保留的内容整理成可管理的长期 Memory，并基于当前用户的真实历史生成带原始证据的长期洞察。

当前版本为可运行的 V1，仅保留文字输入。语音转写、图片和文件输入计划在后续版本评估。

## 核心能力

- 「留一下 / 聊一会儿」双入口
- Moment、Thread 与历史对话恢复
- Companion 短期对话记忆
- Memory Curator 增量提炼、纠正、删除与冲突处理
- Personal Memory RAG 按需召回历史证据
- `Orchestrator → Temporal / Pattern → Verifier → Synthesizer` 洞察链路
- 支持证据、反证和原始 Moment 回溯
- Supabase Auth、PostgreSQL RLS 与 API 双层 `user_id` 隔离
- 条件触发、版本缓存与数据库埋点

## 技术栈

- 客户端：Expo、React Native、TypeScript、Expo Router
- API：FastAPI、Python 3.12
- 数据：Supabase Auth、PostgreSQL、pgvector、pg_trgm、RLS
- 模型：阿里云百炼 OpenAI-compatible API，模型名称通过环境变量配置
- 部署配置：Vercel Web + API，EAS Android Preview

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

## 自动化与模型评测

当前本地基线：

- Python/API/安全测试：16/16 通过
- Expo Doctor：21/21 通过
- Memory Curator：24/24 受控 Case 通过
- Embedding Retrieval：15/15，Recall@3=1.0，MRR=0.9333
- Insight / Bad Case：12/12，Grounded Claim Rate=1.0

模型评测共 51 条受控 Case，覆盖一次性情绪、引用污染、更新/冲突、重复记忆、语义改写、硬负例、时间变化、重复模式、反证、证据不足和 Prompt Injection。它们是小规模 V1 基线，不代表线上大样本效果；评测脚本和测试集位于 `evaluation/`，原始调用报告仅保存在本地。

运行方式：

```powershell
cd apps/api
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app tests ..\..\evaluation
.\.venv\Scripts\python.exe ..\..\evaluation\run_evaluation.py --suite all
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
