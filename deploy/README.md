# 更新线上 Demo

线上保留三个项目：`echotrace-web` 提供界面，`echotrace-api` 接收请求，`echotrace-worker` 持续处理索引和历史分析。数据和登录仍使用原 Supabase 项目。

## 为什么分开

保存记录应快速返回。深度分析可能持续几分钟，不能依赖一次网页请求保持连接。API 将任务存入 PostgreSQL，由 Worker 领取；网页查询任务进度。这里复用现有任务表，没有增加第二套队列。

Worker 保留本地质量版的推理策略、步骤和调用预算。`VERCEL=1` 不会关闭独立 Worker 的质量模式。API 只有设置 `DURABLE_WORKER_ENABLED=true` 才接受后台分析；这是部署配置开关，不是实时健康监测。

## 发布前

- 确认账号、项目、代码版本，并记录当前正式部署 ID，便于回退。
- 先应用缺少的增量迁移，不要在已有数据库重跑 initial。2026-10-25 本次更新应用了 durable_analysis、clear_successful_job_error、product_reliability、product_readiness 四项迁移，源文件位于 `supabase/migrations/`。
- 迁移为旧记录补充版本字段。旧洞察没有证据版本，因此标记为 stale，不再作为当前有效结果展示；原始记录和旧洞察不删除。
- Worker 的服务端密钥只放在该项目 Production 环境。不要放到前端、Git、Docker 构建参数或日志里。
- 先构建候选版本，检查就绪状态，再切换正式地址。构建成功不代表任务可以完成。

## Worker 部署注意

Vercel Daemons 当前要求项目根目录为仓库根目录。API 的根目录仍为 `apps/api`，前端为 `apps/mobile`。不要为了 Worker 改动这两个项目的根目录。

Worker 配置源文件是根目录的 `vercel.worker.json`。请在独立的干净源码副本中将它复制为 **`vercel.json`**，再部署到 `echotrace-worker`。本次实际验证中，仅传 `--local-config vercel.worker.json` 会发布静态页，却没有创建 daemon；不能据此宣布后台已上线。不要在用于部署 API 或前端的源码副本中留下这个根配置。

Docker 使用现有 `Dockerfile`，启动命令为 `python -m app.worker`，新加坡一个 1 GiB 实例。服务端需配置 Supabase URL、anon key、service-role key、百炼凭据、模型与 Embedding 配置。API 与 Worker 应采用兼容的 Embedding 模型和维度。当前 Worker 每 5 秒检查一次任务，任务总时限 600 秒，其他预算由环境变量明确设置。

运行时检查：

```powershell
npm exec --yes --package=vercel@65.2.0 -- vercel daemons list --project echotrace-worker --scope dibuu --json
npm exec --yes --package=vercel@65.2.0 -- vercel logs --project echotrace-worker --scope dibuu --query 'source:daemon' --since 10m
```

日志含任务标识，不应包含记录正文或认证信息。项目静态标识页不是健康检查；必须验证一条任务真正完成。

## 切换与回退

API 和前端使用 `--prod --skip-domain` 先创建候选正式构建，验收后用 `vercel promote <候选地址> --scope dibuu --yes` 切换。回退时可将之前记录的正式构建重新 promote。只回退应用不等于回退数据库；本次迁移是增量结构，不能为回退执行删表或清空用户数据。

完整链路检查至少包括：登录、保存、重复请求返回同一记录、冲突请求返回 409、Worker 完成索引、聊天返回、后台分析完成和结果可读取。使用独立测试账号，不清空原账号数据。旧封存评测不能作为新版上线效果指标。

2026-10-25 本次实际上线检查通过了上述链路。两个新建合成 Moment 的索引均完成，聊天返回 200；三个分析请求中，一个完成并通过结果接口读取，关联两条证据，另两个以依据不足结束。第二个停止案例还暴露了下述否定句分类问题。这是发布验收记录，不是新版本效果评测，不能据此计算准确率或替代旧封存报告。

## 免费方案的边界

当前账号使用 Hobby。Vercel Daemons 仍为 Beta，Hobby 每月有 5 Active CPU-hours，额度耗尽会停止后台服务，不会自动购买套餐。模型调用和 Supabase 另受各自额度限制。详见 [官方限制说明](https://vercel.com/docs/daemons/limits)。这不是长期稳定服务的保证。

线上网页登录仍需访问 Supabase。大陆网络的 DNS、代理和跨境连接可能影响登录；后端可用不代表每个用户的网络都能稳定直连。每周回顾仍在访问时检查并创建任务，没有新增独立定时调度，也没有启用历史自动删除。

本次检查还发现，现有查询分类规则会将含“变化”的否定句也当作变化问题，例如“不作跨时期变化判断”。它可能因此要求并不存在的对照时期证据，最后提示依据不足。此问题已记录，本次发布没有借机修改 AI 判断规则或重新评测。
