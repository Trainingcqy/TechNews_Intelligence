# 开发与运维

本文档描述环境变量、本地开发、部署和采集运维。系统设计见[系统架构](ARCHITECTURE.md)。

## 环境变量

本机和服务器都使用仓库根目录的 `.env`，模板为 `.env.example`，两边的变量名相同：

```bash
cp .env.example .env
```

`app/` 的四个入口 `api`、`trace_api`、`bot`、`cli`，以及 `python -m ingest`、评测脚本和冒烟脚本，经 `common/script_env.py` 的 `load_env()` 读取它，shell 中已设置的变量优先于文件；冒烟脚本和只读数据库的统计脚本可用 `--env-file` 指定其他文件。服务器上，compose 自动读取同目录的 `.env`，把各服务 `environment` 中列出的变量传入容器，容器内没有 `.env` 文件。`scripts/db/` 的脚本也读这个文件。

模板只列必须由部署者填写的变量：密钥，以及随部署环境变化、没有通用默认值的账号、地址和路径。只在服务器使用的变量在模板中是注释掉的。

| 变量 | 用途 | 传给 |
| --- | --- | --- |
| `DB_USER`, `DB_PASS` | 数据库账号，同时是 postgres 容器的 `POSTGRES_USER`、`POSTGRES_PASSWORD`。`DB_NAME` 不填时为 `DB`。本机的 `DB_HOST`、`DB_PORT` 默认为 `127.0.0.1`、`5555`，即经 SSH 隧道连接 | postgres、ingest、bot、api、trace |
| `DEEPSEEK_API_KEY` | 取证模型与采集分析 | ingest、bot、api |
| `JINA_API_KEY` | 向量、重排与 Reader | ingest、bot、api |
| `VERTEX_PROJECT`, `GOOGLE_APPLICATION_CREDENTIALS` | 综合模型。后者是服务账号 JSON 的宿主机路径，compose 把它只读挂载到容器内的 `/run/secrets/gcp-sa.json`，容器内的同名变量固定为这个路径 | bot、api |
| `HTTP_PROXY`, `HTTPS_PROXY` | 不能直连 DeepSeek、Google、Jina 时填写 | ingest、bot、api |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_IDS` | 服务器。后者是允许执行 `/settings`、`/quota` 的用户 ID，逗号分隔 | bot |
| `CORS_ORIGINS`, `API_BASE_URL`, `APPROVE_LINK_SECRET` | 服务器。前端的跨域来源、审批链接使用的公开 API 地址、审批链接的签名密钥 | api |
| `SMTP_USER`, `SMTP_PASS`, `ADMIN_EMAIL` | 服务器。ingest 的告警和 api 的额度审批邮件发到 `ADMIN_EMAIL`，发信服务器为代码默认的 `smtp.gmail.com:587` | ingest、api |
| `TRACE_DASHBOARD_TOKEN` | 服务器。Trace Console 的访问 token，本机运行 Trace Console 时同样需要 | trace |

端口变量不填时取 compose 中的默认值：`DB_PORT` 为 `5432`，`API_PORT` 为 `8000`，`TRACE_DASHBOARD_PORT` 为 `8010`。`DB_PORT` 是 postgres 发布到宿主机的端口，绑定在 `127.0.0.1`；本机不填这个变量。

生成 `APPROVE_LINK_SECRET`、`TRACE_DASHBOARD_TOKEN` 这类随机值：

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 可调参数

可调参数的默认值写在代码里，就是生产取值，不写进模板。本机修改时在 `.env` 中加一行；服务器修改时在 `docker-compose.yml` 对应服务的 `environment` 中加一行。

| 变量 | 默认 | 含义 |
| --- | --- | --- |
| `AGENT_GATHER_PROVIDER`, `AGENT_GATHER_MODEL` | `deepseek`, `deepseek-flash` | 取证模型，须支持工具调用。 |
| `AGENT_FINAL_PROVIDER`, `AGENT_FINAL_MODEL` | `vertex`, `gemini-3.1-pro-preview` | 综合模型；评测的判官也用它。 |
| `AGENT_MAX_STEPS`, `AGENT_HISTORY_TURNS`, `AGENT_TEMPERATURE` | `3`, `4`, `0.1` | 取证循环的最大步数，取值 1 到 6；带入上下文的历史轮数；两个模型的温度。 |
| `INGEST_ANALYZE_PROVIDER`, `INGEST_ANALYZE_MODEL` | `deepseek`, `deepseek-flash` | 采集分析模型。 |
| `INGEST_<字段名大写>` | 见下文 | `IngestConfig` 的任一字段，例如 `INGEST_FETCH_WORKERS`。 |
| `VERTEX_LOCATION` | `global` | Vertex 区域。 |
| `DEEPSEEK_BASE_URL`, `DEEPSEEK_TIMEOUT_SEC` | `https://api.deepseek.com`, `120` | OpenAI 兼容接口的地址与超时秒数。 |
| `GEMINI_API_KEY` | 无 | 某个用途的供应商选 `gemini_api` 时需要。 |
| `NEWS_RERANK_MODE` | `llm_rerank` | 设为 `none` 关闭重排。 |
| `JINA_RERANK_MODEL`, `JINA_RERANK_TIMEOUT_SEC` | `jina-reranker-v3`, `15` | 重排模型与超时秒数。 |
| `RETRIEVAL_POINTS_BOOST`, `RETRIEVAL_FRESHNESS_BOOST`, `RETRIEVAL_TRGM_THRESHOLD` | 关, `0`, `0.42` | 检索排序的热度加权、新鲜度加权，以及精确召回的 pg_trgm 相似度阈值。 |
| `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`, `LANGSMITH_ENDPOINT` | 关 | LangSmith 外部观测；`LANGSMITH_API_KEY` 由 `langsmith` 库读取。 |
| `BOT_SESSION_MAX_CHATS`, `BOT_SESSION_TTL_SEC`, `BOT_RATE_TRACK_MAX_CHATS`, `BOT_RATE_WINDOW_SEC`, `BOT_RATE_LIMIT`, `BOT_AUTO_DELETE_SEC`, `BOT_SEND_RETRY_ATTEMPTS`, `BOT_SEND_RETRY_BASE_SEC` | 见 `app/bot.py` | Telegram Bot 的会话缓存、限流、消息自动删除和发送重试。 |
| `API_RATE_LIMIT`, `API_RATE_TRACK_MAX_IPS`, `APPROVE_LINK_TTL_SEC`, `SMTP_HOST`, `SMTP_PORT` | 见 `app/rate_limit.py`、`app/security.py`、`common/mail.py` | 接口限流、审批链接有效期、发信服务器。 |

模型变量的规则：

- 每个用途只认自己的 `<用途>_PROVIDER` 和 `<用途>_MODEL`；没有给模型名时，取 `common/llm_provider.py` 中 `DEFAULT_MODELS` 的供应商默认值。
- 供应商只认三个名字：`gemini_api` 调 Google AI Studio 上的 Gemini，`vertex` 调 Vertex AI 上的 Gemini，`deepseek` 调 OpenAI 兼容接口。`DEEPSEEK_BASE_URL` 指向其他兼容服务时同样可用，但所有选 `deepseek` 的用途共用一个地址和 key。
- 采集分析只适配 OpenAI 兼容的客户端：失败分类按 openai 的异常类型，`max_tokens` 只发给 `ChatOpenAI`。`response_format` 对 `vertex` 和 `gemini_api` 的效果未验证，供应商换成这两个时把 `INGEST_ANALYZE_JSON_MODE` 设为 `false`。
- 向量模型 `jina-embeddings-v3` 写死在 `ingest/embed.py` 与 `agent/retrieval/embeddings.py`，两处必须一致。

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

## 本地开发

Python 版本为 3.12，与镜像和 CI 一致。安装依赖：

```bash
pip install -r requirements.txt -c constraints.txt
```

`requirements.txt` 声明直接依赖的版本范围，`constraints.txt` 固定全部依赖的精确版本，镜像和 CI 用同一条命令安装。改动 `requirements.txt` 后，在 Python 3.12 的全新虚拟环境中安装它，测试全部通过后用 `pip freeze` 重新生成 `constraints.txt`。

本机连接 `.env` 中配置的数据库，默认经 SSH 隧道连接服务器上的数据库，例如 `ssh -N -L 5555:127.0.0.1:5432 <用户>@<服务器>`。经隧道时，本机直接运行的命令读写的是服务器上的数据：

- `python -m ingest` 的 `run`、`loop`、`relabel`、`requeue`、`sync-sources` 写入采集数据。
- `eval/tracking/run_tracking.py` 写入评测用的关注项。
- CLI 和调用 `run_turn` 的评测脚本写入 Trace。

`scripts/db/` 下的脚本经 docker compose 执行，作用于所在机器上的 compose 服务。

各入口：

```bash
uvicorn app.api:app --host 0.0.0.0 --port 8000         # Web API
uvicorn app.trace_api:app --host 0.0.0.0 --port 8010   # Trace Console 后端
python -m app.bot                                      # Telegram Bot
python -m app.cli                                      # 命令行对话
python -m agent.mcp.stdio_server                       # MCP stdio 服务
python -m ingest run                                   # 采集一轮，命令见「采集运维」
```

`python -m ingest run` 和 `loop` 还会调用 Reader、模型和向量接口。同一个数据库上只有一个采集进程处理文章，已有进程运行时 `run` 退出。

冒烟脚本调用真实模型，由使用者运行并查看输出：

```bash
python scripts/smoke/smoke_agent.py      # 取证模型与综合模型各一次调用，不连数据库
```

两个前端都是 Vue 3 + Vite 工程，Node 版本为 24，与 Trace 镜像和 Pages 构建一致。在各自目录 `npm install` 后用 `npm run dev` 开发、`npm run build` 构建：

| 目录 | 开发服务器端口 | 请求转发 |
| --- | --- | --- |
| `web/chat/` | `5174` | `/api/*` 去掉前缀后转发到 Web API，即 `http://127.0.0.1:8000` |
| `web/trace/` | `5173` | `/trace-api/*` 转发到 Trace Console 后端，即 `http://127.0.0.1:8010` |

网页对话前端的构建产物直接请求 `https://agentapi.trainingcqy.com`，由 Cloudflare Pages 发布，见「自动部署」。Trace Console 的构建产物和 `/trace-api/*` 接口在生产环境由 `trace` 容器提供。

### 测试与 CI

```bash
pytest tests -q
python scripts/encoding_guard.py --root . --report reports/encoding_guard/local.json
```

测试不读取开发者的 `.env`：`tests/conftest.py` 把 `common.script_env.load_env` 替换为空操作。采集解析层的录制样本在 `tests/fixtures/ingest/`。

CI 工作流 `.github/workflows/ci.yml` 在 Python 3.12 上安装依赖，运行编码检查和 `pytest tests -v`，并上传编码检查报告；编码检查失败不阻断。

### 代码组织

- 业务入口在 `app/`。
- Agent 编排、工具和检索在 `agent/`，模块间的依赖边界见[系统架构](ARCHITECTURE.md)的「运行边界」。
- 采集管线在 `ingest/`：解析和判定逻辑是不访问网络与数据库的纯函数，HTTP 请求经 `ingest/http.py`，模型调用在 `ingest/analyze.py`，SQL 集中在 `ingest/store.py`。
- 采集、Agent、入口共用的模块在 `common/`。
- `news` schema 的结构变更写入 `sql/news.sql`，其余表的结构变更写入 `sql/public.sql`。
- 部署和数据库运维脚本放入 `scripts/`，评测逻辑放入 `eval/`，测试放入 `tests/unit/`。
- 运行产物不纳入版本控制，包括 `reports/`、缓存、构建产物和虚拟环境。

## 部署

Compose 文件是仓库根目录的 `docker-compose.yml`，项目名用 `name: deployment` 固定，PostgreSQL 的数据卷 `deployment_pg_data` 因此不随目录名变化。本文档中的 `docker compose` 命令都在仓库根目录执行。

| 服务 | 容器 | 端口 | 职责 |
| --- | --- | ---: | --- |
| `postgres` | `tech_news_db` | `5432` | PostgreSQL + pgvector。 |
| `ingest` | `tech_news_ingest` | 无 | 采集管线，命令 `python -m ingest loop`。 |
| `api` | `tech_news_api` | `8000` | Web API。 |
| `trace` | `tech_news_trace` | `8010` | Trace Console。 |
| `bot` | `tech_news_bot` | 无 | Telegram Bot。 |

所有服务都配置了 `restart: always`。查看状态与日志：

```bash
docker compose ps
docker compose logs -f ingest
```

### 自动部署

`main` 分支推送后，GitHub Actions 按 `.github/workflows/deploy.yml` 经 SSH 在服务器上部署：拉取代码，拉取并启动 `postgres`，执行 `bash scripts/db/apply_schema.sh`，构建 `bot`、`api`、`trace`、`ingest`，执行 `docker compose up -d --remove-orphans`，清理镜像，最后检查各服务的状态。PostgreSQL 镜像摘要变化时用 `REINDEX SCHEMA CONCURRENTLY` 重建 `news` 与 `public` 两个 schema 的索引。自动部署不执行 seed。

`up -d` 会重新启动已手动停止的 `ingest`，服务器重启时 `restart: always` 同样会启动它。需要在 `ingest` 停止的状态下操作时，例如冻结评测用的数据快照，期间不要推送 `main`，也不要重启服务器。

网页对话前端 `web/chat/` 由 Cloudflare Pages 从 `main` 分支构建并发布，域名为 `agent.trainingcqy.com`。Pages 项目的构建设置：根目录 `web/chat`，构建命令 `npm run build`，输出目录 `dist`；Node 版本取 `web/chat/.node-version`。

### 表结构

`bash scripts/db/apply_schema.sh` 依次执行 `sql/public.sql`、`sql/news.sql`，可重复执行，由自动部署调用。

### Cloudflare Tunnel

两个域名经 Tunnel 指向宿主机端口，API 和 Trace 服务只绑定宿主机本地地址：

| 域名 | Origin |
| --- | --- |
| `agentapi.trainingcqy.com` | `http://localhost:8000` |
| `trace.trainingcqy.com` | `http://localhost:8010` |

### Trace Console

浏览器访问 `trace.trainingcqy.com` 或本地 `http://localhost:8010`，输入 `TRACE_DASHBOARD_TOKEN` 后进入面板。面板内容见[系统架构](ARCHITECTURE.md)的「观测」。

## 采集运维

`ingest` 容器常驻运行 `python -m ingest loop`，每轮结束后等待 300 秒。各阶段与状态机见[系统架构](ARCHITECTURE.md)的「采集」。命令：

| 命令 | 作用 |
| --- | --- |
| `python -m ingest sync-sources` | 把 `sources.toml` 同步到数据库 |
| `python -m ingest run [--stage <名称>] [--channel <键>]` | 执行一轮，不执行 monitor；`--channel` 只限定 discover 轮询的渠道 |
| `python -m ingest loop [--interval 300]` | 常驻运行 |
| `python -m ingest status` | 渠道最近一次成功轮询、`ready` 以外各状态的积压数、近 24 小时的失败分组 |
| `python -m ingest requeue --status failed [--error-kind <类型>]` | 把 `failed` 的文章恢复到失败前的阶段 |
| `python -m ingest monitor [--dry-run]` | 计算契约指标并发告警；`--dry-run` 只记日志 |
| `python -m ingest relabel [--limit N] [--keys-file <文件>]` | 对已有摘要的文章重新标注领域与类型 |

服务器上在容器内执行，例如：

```bash
docker compose run --rm ingest python -m ingest status
docker compose run --rm ingest python -m ingest requeue --status failed --error-kind reader_http
```

`run` 与 `loop` 共用一个 PostgreSQL advisory lock，常驻容器在运行时手动执行 `run` 会打印"已有采集进程在运行"并以退出码 1 结束；`--channel` 指定的渠道不存在或未启用时以退出码 2 结束。其余命令不取锁。

告警邮件发往 `ADMIN_EMAIL`：渠道连续失败 3 次时发一封；`monitor` 的指标越限时发一封，列出越限项和当日全部指标。SMTP 未配置或收件人为空时只记日志。

### 重新标注

`relabel` 对已有摘要、且标签来自旧分类映射或尚未记录词表版本的文章重新标注领域与类型，`--keys-file` 把范围限定为文件中列出的 `url_key`，`--limit` 限定篇数。

### 来源维护

发布方和渠道定义在 `ingest/sources.toml`，它是唯一真源。

- 增加渠道：在文件中加一段 `[[channel]]`，渠道引用的发布方须是文件中已登记的发布方。
- 停用渠道：保留该段并写 `enabled = false`，或从文件中删除该段，同步后库内对应行的 `enabled` 置为假；行和已有的发现记录保留。
- `hosts` 的每一项是域名，或域名加路径前缀；文件中登记项重复、渠道键重复、渠道引用不存在的发布方时读取报错。
- 同步只写定义列：发布方的 `kind` 与登记项，渠道的名称、类型、入口、用途、发布方、语言、`enabled` 和 `config`。轮询间隔 `poll_minutes` 默认 60 分钟，它和运行状态列不受同步影响；某个渠道需要不同的轮询间隔时直接修改库内的 `poll_minutes`。
- 渠道级参数在 `config` 中：`body_from_feed`、`lookback_days`、`url_pattern`、`max_path_segments`、`discussion_after_hours`、`discussion_min_comments`。

`sources.toml` 随镜像发布。修改后经部署或 `docker compose build ingest` 进入镜像，再执行 `docker compose run --rm ingest python -m ingest sync-sources`。
