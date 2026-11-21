# 部署与运维

本文档描述生产和本地部署、数据库脚本、端口、Cloudflare Tunnel、采集管线、一次性切换与回退、n8n 存档、Metabase、Trace Console 和常用运维命令。

## Docker Compose 服务

Compose 文件是仓库根目录的 `docker-compose.yml`，项目名用 `name: deployment` 固定，PostgreSQL 的数据卷 `deployment_pg_data` 因此不随目录名变化。本文档中的 `docker compose` 命令都在仓库根目录执行。

| 服务 | 容器 | 端口 | 职责 |
| --- | --- | ---: | --- |
| `postgres` | `tech_news_db` | `5432` | PostgreSQL + pgvector。 |
| `ingest` | `tech_news_ingest` | 无 HTTP 端口 | 采集管线，命令 `python -m ingest loop`。 |
| `metabase` | `tech_news_bi` | `3000` | BI 看板。 |
| `api` | `tech_news_api` | `8000` | FastAPI 主入口。 |
| `trace` | `tech_news_trace` | `8010` | Trace Console 服务。 |
| `bot` | `tech_news_bot` | 无 HTTP 端口 | Telegram Bot。 |

所有服务都配置了 `restart: always`。

启动服务：

```bash
docker compose up -d
```

查看状态：

```bash
docker compose ps
```

查看日志：

```bash
docker compose logs -f ingest
docker compose logs -f api
docker compose logs -f trace
docker compose logs -f bot
```

## 环境变量

环境变量模板是仓库根目录的 `.env.example`。本机和服务器都使用仓库根目录的 `.env`，两边的变量名相同。

```bash
cp .env.example .env
```

模板只列必须由部署者填写的变量：密钥，以及随部署环境变化、没有通用默认值的账号、地址和路径；只在服务器填写的变量在模板中是注释掉的。可调参数的默认值写在代码里，就是生产取值，不写进模板；需要修改时，在 `docker-compose.yml` 对应服务的 `environment` 中加一行。compose 自动读取同目录的 `.env` 并按服务传入容器，容器内没有 `.env` 文件。`deployment/scripts/db/` 的脚本也读这个文件。

必填项以及它们传给的服务：

| 变量 | 传给 |
| --- | --- |
| `DB_USER`, `DB_PASS` | 同时是 postgres 容器的 `POSTGRES_USER`、`POSTGRES_PASSWORD`，并传给 ingest、bot、api、trace。`DB_NAME` 不填时为 `DB`，同样用于 postgres 容器的 `POSTGRES_DB` 并传给这四个服务。 |
| `DEEPSEEK_API_KEY`, `JINA_API_KEY` | ingest、bot、api。取证模型与采集分析用 DeepSeek；向量、重排和 Reader 用 Jina。 |
| `VERTEX_PROJECT`, `GOOGLE_APPLICATION_CREDENTIALS` | bot、api。综合模型用 Vertex AI。`GOOGLE_APPLICATION_CREDENTIALS` 是服务账号 JSON 的宿主机路径，只读挂载到容器内的 `/run/secrets/gcp-sa.json`，容器内的同名变量在 compose 中固定为这个路径。 |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_IDS` | bot。`TELEGRAM_ADMIN_IDS` 是允许执行 `/settings`、`/quota` 的用户 ID，逗号分隔。 |
| `CORS_ORIGINS`, `API_BASE_URL`, `APPROVE_LINK_SECRET` | api。前端的跨域来源、审批链接使用的公开 API 地址、审批链接的签名密钥。 |
| `SMTP_USER`, `SMTP_PASS`, `ADMIN_EMAIL` | `SMTP_USER`、`SMTP_PASS` 传给 ingest、api；`ADMIN_EMAIL` 传给 ingest、api、trace。ingest 的告警和 api 的订阅审批邮件发到 `ADMIN_EMAIL`，发信服务器为代码默认的 `smtp.gmail.com:587`。 |
| `TRACE_DASHBOARD_TOKEN` | trace。Trace Console 管理员访问 token。 |
| `HTTP_PROXY`, `HTTPS_PROXY` | ingest、bot、api。服务器不能直连 DeepSeek、Google、Jina 时填写。 |

端口变量，不填时取 compose 中的默认值：

| 变量 | 默认值 |
| --- | ---: |
| `DB_PORT` | `5432` |
| `METABASE_PORT` | `3000` |
| `API_PORT` | `8000` |
| `TRACE_DASHBOARD_PORT` | `8010` |

`DB_PORT` 是 postgres 发布到宿主机的端口，绑定在 `127.0.0.1`。本机运行时不填这个变量，默认 `5555`，即 SSH 隧道。

生成 `APPROVE_LINK_SECRET`、`TRACE_DASHBOARD_TOKEN` 这类随机值：

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 可调参数

| 变量 | 默认 | 含义 |
| --- | --- | --- |
| `AGENT_GATHER_PROVIDER`, `AGENT_GATHER_MODEL` | `deepseek`, `deepseek-flash` | 取证模型，须支持工具调用。 |
| `AGENT_FINAL_PROVIDER`, `AGENT_FINAL_MODEL` | `vertex`, `gemini-3.1-pro-preview` | 综合模型；评测的判官也用它。 |
| `AGENT_MAX_STEPS`, `AGENT_HISTORY_TURNS`, `AGENT_TEMPERATURE` | `3`, `4`, `0.1` | 取证循环的最大步数（1 到 6）、带入上下文的历史轮数、两个模型的温度。 |
| `INGEST_ANALYZE_PROVIDER`, `INGEST_ANALYZE_MODEL` | `deepseek`, `deepseek-flash` | 采集分析模型。 |
| `INGEST_<字段名大写>` | 见下表 | `IngestConfig` 的任一字段，例如 `INGEST_FETCH_WORKERS`。 |
| `VERTEX_LOCATION` | `global` | Vertex 区域。 |
| `DEEPSEEK_BASE_URL`, `DEEPSEEK_TIMEOUT_SEC` | `https://api.deepseek.com`, `120` | OpenAI 兼容接口的地址与超时秒数。 |
| `GEMINI_API_KEY` | 无 | 某个用途的供应商选 `gemini_api` 时需要。 |
| `NEWS_RERANK_MODE` | `llm_rerank` | 设为 `none` 关闭重排。 |
| `JINA_RERANK_MODEL`, `JINA_RERANK_TIMEOUT_SEC` | `jina-reranker-v3`, `15` | 重排模型与超时秒数。 |
| `RETRIEVAL_POINTS_BOOST`, `RETRIEVAL_FRESHNESS_BOOST`, `RETRIEVAL_TRGM_THRESHOLD` | 关, `0`, `0.42` | 检索排序的热度加权、新鲜度加权，以及精确召回的 pg_trgm 相似度阈值。 |
| `AGENT_TRACE_FULL_MODEL_IO`, `AGENT_TRACE_SECRET_REDACTION` | 开, 开 | Trace 是否记录完整的模型输入输出、是否对疑似凭据的内容脱敏。 |
| `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`, `LANGSMITH_ENDPOINT` | 关 | LangSmith 外部观测；`LANGSMITH_API_KEY` 由 `langsmith` 库读取。 |
| `TRACE_DASHBOARD_ADMIN_EMAIL` | 同 `ADMIN_EMAIL` | Trace Console 接口返回的管理员邮箱。 |
| `BOT_SESSION_MAX_CHATS`, `BOT_SESSION_TTL_SEC`, `BOT_RATE_TRACK_MAX_CHATS`, `BOT_RATE_WINDOW_SEC`, `BOT_RATE_LIMIT`, `BOT_AUTO_DELETE_SEC`, `BOT_SEND_RETRY_ATTEMPTS`, `BOT_SEND_RETRY_BASE_SEC` | 见 `app/bot.py` | Telegram Bot 的会话缓存、限流、消息自动删除和发送重试。 |
| `API_RATE_LIMIT`, `API_RATE_TRACK_MAX_IPS`, `APPROVE_LINK_TTL_SEC`, `SMTP_HOST`, `SMTP_PORT` | 见 `app/rate_limit.py`、`app/security.py`、`services/mail.py` | 接口限流、审批链接有效期、发信服务器。 |
| `DEEPSEEK_ENTITY_MODEL`, `DEEPSEEK_ENTITY_ESCALATION_MODEL`, `DEEPSEEK_ENTITY_TIMEOUT_SEC` | `deepseek-flash`, `deepseek-v4-pro`, `30` | 实体别名脚本，读仓库根目录的 `.env`。 |

模型变量的规则：每个用途只认自己的 `<用途>_PROVIDER` 和 `<用途>_MODEL`。供应商只认 `gemini_api`、`vertex`、`deepseek` 三个名字；没有给模型名时，取 `services/llm_provider.py` 中 `DEFAULT_MODELS` 的供应商默认值。`gemini_api` 调 Google AI Studio 上的 Gemini，`vertex` 调 Vertex AI 上的 Gemini，`deepseek` 调 OpenAI 兼容接口；`DEEPSEEK_BASE_URL` 指向其他兼容服务时同样可用，但所有选 `deepseek` 的用途共用一个地址和 key。采集分析只适配 OpenAI 兼容的客户端：失败分类按 openai 的异常类型，`max_tokens` 只发给 `ChatOpenAI`；`response_format` 对 `vertex` 和 `gemini_api` 的效果未验证，供应商换成这两个时把 `INGEST_ANALYZE_JSON_MODE` 设为 `false`。向量模型 `jina-embeddings-v3` 写死在 `ingest/embed.py` 与 `agent/retrieval/embeddings.py`，两处必须一致。

`IngestConfig` 的字段，环境变量为 `INGEST_` 加字段名大写，退避表写成逗号分隔的分钟数：

| 字段 | 默认 | 含义 |
| --- | --- | --- |
| `discover_workers`, `fetch_workers`, `analyze_workers` | `8`, `3`, `4` | 各阶段的线程数。 |
| `fetch_limit`, `analyze_limit`, `discuss_limit` | `60`, `60`, `20` | 每轮处理的上限。 |
| `embed_batch_size` | `32` | embed 每批篇数。 |
| `discover_timeout`, `reader_timeout`, `embed_timeout` | `30`, `60`, `30` | 请求超时，秒。discover 的全部请求和 discuss 的回访请求用 `discover_timeout`。 |
| `fetch_max_attempts`, `analyze_max_attempts`, `embed_max_attempts` | `5`, `3`, `5` | 各阶段的最大尝试次数。 |
| `backoff_minutes` | `10,60,360,1440` | 失败后等待的分钟数，超出表长时取最后一项。 |
| `analyze_body_chars` | `30000` | 送入分析的正文字符数上限。 |
| `analyze_provider`, `analyze_model` | `deepseek`, `deepseek-flash` | 同 `INGEST_ANALYZE_PROVIDER`、`INGEST_ANALYZE_MODEL`。 |
| `analyze_json_mode` | `true` | 请求 `response_format: json_object`。解析与校验在两种模式下相同。 |

## 数据库结构与 Seed

数据库结构脚本：

```bash
bash deployment/scripts/db/apply_schema.sh
```

该脚本依次执行：

- `sql/infrastructure/schema/schema_ddl.sql`
- `sql/infrastructure/schema/news_schema.sql`
- `sql/infrastructure/views/view_dashboard_news.sql`

`schema_ddl.sql` 中的 `public.user_follow` 和 `public.user_story_seen` 保存用户的关注项与阅读记录，随该脚本创建，没有 seed。

Seed 脚本：

```bash
bash deployment/scripts/db/apply_seed.sh
```

执行指定 seed：

```bash
bash deployment/scripts/db/apply_seed.sh --seed-file sql/infrastructure/seeds/seed_entity_core.sql
```

`apply_schema.sh` 用于自动部署，`apply_seed.sh` 用于手动维护实体和别名基础数据。来源不走 seed，见「来源维护」。

## 自动部署

GitHub Actions 工作流位于 `.github/workflows/deploy.yml`。`main` 分支推送后通过 SSH 进入服务器执行部署。

部署顺序：

1. `git pull --ff-only origin main`
2. 拉取基础镜像：`postgres`、`metabase`
3. 启动 `postgres`
4. 执行 `bash deployment/scripts/db/apply_schema.sh`
5. 构建 `bot`、`api`、`trace`、`ingest`
6. `docker compose up -d --remove-orphans`
7. 清理镜像；PostgreSQL 镜像摘要变化时执行 `REINDEX INDEX unique_url`
8. 检查 `postgres`、`bot`、`api`、`trace`、`ingest` 的状态

自动部署不会执行 seed。

`up -d` 会重新启动已手动停止的 `ingest`，服务器重启时 `restart: always` 同样会拉起它。需要在 `ingest` 停止的状态下操作（回填旧数据、冻结评测用的数据快照）时，期间不要推送 `main`，也不要重启服务器。

## 一次性切换

部署只做一次：全部代码合并到 main 后，按下表一次部署、一次切换，新旧采集管线不并行运行。

部署前在服务器上用 `docker volume ls` 核对 PostgreSQL 的数据卷名为 `deployment_pg_data`。名称不同时，把 `docker-compose.yml` 的 `name` 改成实际的项目名，否则 compose 会新建一个空的数据卷。

| 步骤 | 操作 | 说明 |
| --- | --- | --- |
| 1 | 在仓库根目录建 `.env`：按「环境变量」由原先放在 `deployment/` 目录中的 `.env` 改写，原文件留作回退用的副本 | `POSTGRES_USER`、`POSTGRES_PASSWORD`、`POSTGRES_DB` 改为 `DB_USER`、`DB_PASS`、`DB_NAME`，`POSTGRES_PORT` 改为 `DB_PORT`，`GCP_SA_PATH` 改为 `GOOGLE_APPLICATION_CREDENTIALS`；`docker compose config` 能解析，没有未设置变量的警告 |
| 2 | 推送 main，部署流程自动执行 | `deploy.yml` 先执行 `apply_schema.sh` 建 schema `news`，旧表不动；再构建并启动全部服务。compose 中已没有 n8n，`up -d --remove-orphans` 删除 n8n 容器，旧采集管线随之停止。此时 `ingest` 已在运行，但库中还没有渠道和文章，各阶段空跑；bot 与 api 已是新 Agent，第 6 步完成前检索不到文章 |
| 3 | `docker compose stop ingest` | 第 4 到 6 步期间常驻采集不运行 |
| 4 | `docker compose run --rm ingest python -m ingest sync-sources` | 写入发布方与渠道 |
| 5 | `docker compose run --rm ingest python -m ingest migrate --dry-run`，核对行数后去掉 `--dry-run` 执行 | 回填历史 |
| 6 | `docker compose run --rm ingest python -m ingest run --stage link` | 为全部历史文章分配故事，约 2.3 万次最近邻查询 |
| 7 | `docker compose start ingest` | 新管线开始采集 |
| 8 | 旧文章的标签 | `migrate` 已按旧分类做机械映射；是否用 `relabel` 全量重标由分类试标的结果决定，见「旧表回填与重标」与 [评测体系](EVALUATION.md) |

第 3 到 7 步期间不推送 main，也不重启服务器，原因见「自动部署」。

**切换之后**：旧表不再更新，迁移带来的历史截止到第 2 步。新管线第一轮补上 feed 和 sitemap 中仍在保留范围内的条目，更早的空档不补。Metabase 看板读旧视图，数据停在切换日。

**回退**：把 main 回退到切换前的提交并推送，部署流程按切换前的 compose 文件重建，n8n 容器连同它的数据卷恢复；回退前把原 `deployment/.env` 放回。旧表在整个过程中没有被修改，schema `news` 不影响旧系统。

## Cloudflare Tunnel

推荐映射：

| 域名 | Origin |
| --- | --- |
| `dashboard.trainingcqy.com` | `http://localhost:3000` |
| `agentapi.trainingcqy.com` | `http://localhost:8000` |
| `trace.trainingcqy.com` | `http://localhost:8010` |

API 和 Trace 服务默认只绑定宿主机本地地址，适合通过 Tunnel 暴露。

## 采集管线

`ingest` 容器常驻运行 `python -m ingest loop`，每轮结束后等待 300 秒。架构与各阶段见 [系统架构](ARCHITECTURE.md)。管理命令在容器内执行：

```bash
# 各渠道最近一次成功轮询、积压数、近 24 小时失败分组
docker compose run --rm ingest python -m ingest status

# 修复某类失败的原因之后，把 failed 的文章恢复到失败前的阶段
docker compose run --rm ingest python -m ingest requeue --status failed --error-kind reader_http

# 只轮询一个渠道，其余阶段照常处理
docker compose run --rm ingest python -m ingest run --channel openai_news

# 计算契约指标；--dry-run 只记日志，不发邮件
docker compose run --rm ingest python -m ingest monitor --dry-run
```

`run` 与 `loop` 共用一个 PostgreSQL advisory lock，常驻容器在运行时手动执行 `run` 会打印"已有采集进程在运行"并以退出码 1 结束；`--channel` 指定的渠道不存在或未启用时以退出码 2 结束。`sync-sources`、`status`、`requeue`、`monitor`、`migrate`、`relabel` 不取锁。

告警邮件发往 `ADMIN_EMAIL`：渠道连续失败 3 次时发一封；`monitor` 的指标越限时发一封，列出越限项和当日全部指标。SMTP 未配置或收件人为空时只记日志。

### 旧表回填与重标

`migrate` 把旧表（`tech_news`、`tech_news_failed`、`jina_raw_logs`、`news_embeddings`）的数据写入 `news` schema，可以重复执行，已存在的行跳过。`migrate` 不自动同步来源，映射到的渠道在 `news.channel` 中缺失时报错退出，因此先执行 `sync-sources`。

```bash
docker compose stop ingest
docker compose run --rm ingest python -m ingest sync-sources
docker compose run --rm ingest python -m ingest migrate --dry-run
docker compose run --rm ingest python -m ingest migrate
docker compose run --rm ingest python -m ingest run --stage link
docker compose start ingest
```

`--dry-run` 只打印各步将写入的行数；`run --stage link` 为迁入的文章分配故事。`relabel` 对已有摘要、且标签来自旧分类映射或尚未记录词表版本的文章重新标注领域与类型，`--keys-file` 把范围限定为文件中列出的 `url_key`，`--limit` 限定篇数。

### 来源维护

发布方和渠道定义在 `ingest/sources.toml`，它是唯一真源。

- 增加渠道：在文件中加一段 `[[channel]]`，渠道引用的发布方须是文件中已登记的发布方。
- 停用渠道：保留该段并写 `enabled = false`，或从文件中删除该段，同步后库内对应行的 `enabled` 置为假；行和已有的发现记录保留。
- `hosts` 的每一项是域名，或域名加路径前缀；文件中登记项重复、渠道键重复、渠道引用不存在的发布方时读取报错。
- 同步只写定义列：发布方的 `kind` 与登记项，渠道的名称、类型、入口、用途、发布方、语言、`enabled` 和 `config`。轮询间隔 `poll_minutes`（默认 60 分钟）和运行状态列不受同步影响，某个渠道需要不同的轮询间隔时直接修改库内的 `poll_minutes`。
- 渠道级参数在 `config` 中：`body_from_feed`、`lookback_days`、`url_pattern`、`max_path_segments`、`discussion_after_hours`、`discussion_min_comments`。

`sources.toml` 随镜像发布。修改后经部署（或 `docker compose build ingest`）进入镜像，再执行：

```bash
docker compose run --rm ingest python -m ingest sync-sources
```

## n8n

n8n 已从 compose 中删除，不再部署。采集由 `ingest` 承担，`etl_workflow/` 中的六个工作流导出文件（`Tech_Intelligence`、`AI_Analysis_Service`、`Daily_Tech_Brief`、`Quality_Monitor`、`System_Alert_Service`、`Eval_Analysis_Regression`）是旧采集链路的存档。

## Metabase

Metabase 使用 `view_dashboard_news` 和 `sql/analytics/` 中的查询 SQL 构建看板。该视图读取旧表 `tech_news`，旧表不再更新，看板的数据停在切换日。Metabase 的应用库由 `MB_DB_FILE=/metabase-data/metabase.db` 指定，宿主机的 `deployment/` 目录挂载为容器内的 `/metabase-data`。看板侧不直接复写北京时间、来源归一和搜索字段逻辑。

## Source 与 Entity 维护

实体别名候选生成，候选来自 `view_dashboard_news`：

```bash
bash deployment/scripts/db/build_entity_alias_candidates.sh --days 14 --limit 1000 --use-deepseek
```

执行前确保 schema/view：

```bash
bash deployment/scripts/db/build_entity_alias_candidates.sh --apply-schema --days 14 --limit 1000 --use-deepseek
```

## 数据质量检查

采集管线的质量由 `ingest` 自带的两个命令检查：`python -m ingest status` 看积压和近 24 小时的失败分类，`python -m ingest monitor` 计算契约指标并与此前 14 天的中位数比较，见「采集管线」。

数据库脚本检查旧表，旧表不再更新后只反映停更前的数据：

```bash
bash deployment/scripts/db/run_data_quality_checks.sh
bash deployment/scripts/db/run_data_quality_checks.sh 48
```

检查项包括最近采集数量、空标题、空摘要、空 URL、重复 URL、向量覆盖情况、来源分布、全文和结构化字段完整性。

## Trace Console

Trace Console 服务由 `trace` 容器提供。浏览器访问 `trace.trainingcqy.com` 或本地 `http://localhost:8010`，输入 `TRACE_DASHBOARD_TOKEN` 后进入面板。

面板展示：

- 请求列表。
- Span 调用链：流程节点、模型调用、工具执行。
- 模型输入 messages 和 raw output。
- 工具参数、结果摘要、证据地址和 diagnostics。
- 错误码、异常链和 raw JSON。

## 常用命令

```bash
# 更新 schema/view
bash deployment/scripts/db/apply_schema.sh

# 手动 seed
bash deployment/scripts/db/apply_seed.sh

# 采集状态
docker compose run --rm ingest python -m ingest status

# 数据质量检查（旧表）
bash deployment/scripts/db/run_data_quality_checks.sh

# 查看采集日志
docker compose logs -f ingest

# 查看 API 日志
docker compose logs -f api

# 查看 Trace 日志
docker compose logs -f trace

# 重启服务
docker compose restart api trace bot ingest
```
