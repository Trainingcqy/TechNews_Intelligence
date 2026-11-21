<div align="center">
  <img src="https://raw.githubusercontent.com/Trainingdlu/TechNews_Intelligence/main/assets/svg/title.svg" alt="TechNews Intelligence" width="700">
  <br>
  <p align="center">
    <a href="https://dashboard.trainingcqy.com" style="text-decoration:none"><strong>Metabase 演示</strong></a>
    &nbsp;&nbsp; | &nbsp;&nbsp;
    <a href="https://agent.trainingcqy.com" style="text-decoration:none"><strong>智能体交互</strong></a>
  </p>
</div>

TechNews Intelligence 是面向科技新闻的自动化情报系统。采集管线 `ingest` 轮询官方渠道、科技媒体和 HackerNews，获取正文并生成摘要、领域与类型标签、向量，使用 PostgreSQL + pgvector 存储，并提供可检索、可追踪、可评测的 Agent 分析能力。用户可以通过 Web API、Trace Console、Telegram Bot、MCP stdio 服务和本地 CLI 使用系统。

![效果展示](https://raw.githubusercontent.com/Trainingdlu/TechNews_Intelligence/main/assets/previews/showcase.png)

## 核心能力

- 新闻采集：采集管线 `ingest/` 每 5 分钟轮询 32 个语料渠道（14 个官方渠道、17 个科技媒体、HackerNews 首页）和 1 个参照渠道。来源定义在 `ingest/sources.toml`，新增来源只需加一段配置并同步。
- 文本处理：feed 自带全文，否则经 Jina Reader 获取正文；模型一次调用生成中文标题、摘要、领域标签和文章类型；摘要生成向量；报道同一事件的文章归并为故事；HackerNews 讨论以原文存储。
- 混合检索：词法、pgvector 语义、实体别名精确三路召回 + RRF 融合，Jina Reranker 重排，结果按故事归并。
- Agent 分析：基于 LangGraph StateGraph 编排。取证模型调用 `search_news`、`read_articles` 两个工具，一步内的多个调用并行执行；证据登记表为每份材料编号；综合模型按编号引用，代码校验引用并生成来源列表。
- 上下文：带入最近几轮对话，历史回答的来源重新登记为证据。
- 可观测性：`agent_runs`、`agent_trace_spans`、`agent_model_io` 记录请求摘要、执行链路和完整模型输入输出。
- 采集监控：渠道连续失败触发告警邮件；每日统计契约指标，超出滚动基线时告警。
- 多入口交互：Web API、Telegram Bot、本地 CLI、MCP stdio 共用同一套检索与工具层。
- 评测：检索质量、生成忠实度、RGB 负样本拒答三项评测，验收问题集，以及数据层的分类试标、故事阈值校准、来源覆盖等统计脚本，并支持 Trace 回溯。

## 系统组成

| 模块 | 说明 |
| --- | --- |
| `ingest/` | 采集管线：来源定义、发现、取正文、摘要与分类、向量、故事归并、HackerNews 讨论、监控。 |
| `agent/` | Agent 编排、两个工具、检索、证据登记表、引用校验、线程上下文、Trace、MCP 适配。 |
| `app/` | FastAPI 主服务、Trace Console 服务、Telegram Bot、本地 CLI。 |
| `services/` | PostgreSQL 连接、对话线程、Trace 持久化、邮件、模型工厂、地址归一、领域与类型词表。 |
| `trace_dashboard/` | Vue3/Vite Trace 可视化面板。 |
| `frontend/` | 静态落地页与订阅页面。 |
| `eval/` | 评测与验收脚本、Trace 查询、编码守卫；`legacy/` 存放归档的评测。 |
| `sql/` | 数据库 schema、视图、seed、分析 SQL。 |
| `docker-compose.yml`、`.env.example` | 仓库根目录的 Docker Compose 配置与环境变量模板。 |
| `deployment/` | 数据库运维脚本、冒烟脚本。 |
| `etl_workflow/` | 旧采集链路的 n8n 工作流导出文件，仅作存档。 |
| `docs/` | 架构、部署、开发和评测文档。 |

## 快速启动

```bash
git clone https://github.com/Trainingdlu/TechNews_Intelligence.git
cd TechNews_Intelligence
cp .env.example .env
```

本机和服务器都使用仓库根目录的 `.env`。编辑 `.env`，填写模板中未注释的变量：

- `DB_USER`、`DB_PASS`
- `DEEPSEEK_API_KEY`、`JINA_API_KEY`
- `VERTEX_PROJECT`、`GOOGLE_APPLICATION_CREDENTIALS`

服务器上另需取消模板中注释掉的变量并填写，主要是：

- `TRACE_DASHBOARD_TOKEN`
- `TELEGRAM_BOT_TOKEN`、`TELEGRAM_ADMIN_IDS`
- `CORS_ORIGINS`、`API_BASE_URL`、`APPROVE_LINK_SECRET`、`SMTP_USER`、`SMTP_PASS`、`ADMIN_EMAIL`

各变量的含义和可调参数见 [部署与运维](docs/OPERATIONS.md)。本机开发的 `.env` 见 [开发说明](docs/DEVELOPMENT.md)。

启动服务：

```bash
docker compose up -d
```

初始化或更新数据库结构：

```bash
bash deployment/scripts/db/apply_schema.sh
```

手动导入 seed：

```bash
bash deployment/scripts/db/apply_seed.sh
```

把 `ingest/sources.toml` 中的来源同步到数据库，`ingest` 随后开始轮询：

```bash
docker compose run --rm ingest python -m ingest sync-sources
```

## 常用服务与端口

| 服务 | 容器 | 默认端口 | 说明 |
| --- | --- | --- | --- |
| PostgreSQL | `tech_news_db` | `5432` | 数据库与向量检索。 |
| ingest | `tech_news_ingest` | 无 HTTP 端口 | 采集管线。 |
| Metabase | `tech_news_bi` | `3000` | 数据分析看板。 |
| API | `tech_news_api` | `8000` | Web/程序调用入口。 |
| Trace Console | `tech_news_trace` | `8010` | Agent 链路追踪面板。 |
| Telegram Bot | `tech_news_bot` | 无 HTTP 端口 | Telegram 对话入口。 |

Cloudflare Tunnel 可按需映射：

| 域名 | Origin |
| --- | --- |
| `agentapi.trainingcqy.com` | `http://localhost:8000` |
| `trace.trainingcqy.com` | `http://localhost:8010` |
| `dashboard.trainingcqy.com` | `http://localhost:3000` |

## 常用命令

```bash
# 单元测试
pytest tests/unit -q

# Trace 面板构建
cd trace_dashboard
npm run build

# 本地 CLI
python -m app.cli

# 采集状态：渠道最近一次成功轮询、积压数、近 24 小时失败分组
python -m ingest status

# 查询一次 Agent Trace
python eval/trace_query.py --request-id <request_id>
```

## 项目文档

- [系统架构](docs/ARCHITECTURE.md)
- [部署与运维](docs/OPERATIONS.md)
- [开发说明](docs/DEVELOPMENT.md)
- [评测体系](docs/EVALUATION.md)

## License

本项目用于科技新闻情报系统的采集、分析、展示和评测。第三方服务、模型和数据源的使用需遵守各自服务条款。
