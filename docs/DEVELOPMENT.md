# 开发说明

本文档描述本地开发、服务启动、测试、前端构建、CI 和目录结构。

## 本地环境

Python 版本：3.12，与镜像和 CI 一致。

安装依赖：

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt -c constraints.txt
```

`requirements.txt` 声明直接依赖的版本范围，`constraints.txt` 固定 `requirements.txt` 及其全部依赖的精确版本。镜像和 CI 都用上面的命令安装。改动 `requirements.txt` 后，在 Python 3.12 的全新虚拟环境中安装它，`tests/unit` 全部通过后用 `pip freeze` 重新生成 `constraints.txt`。

### 环境变量

本机的环境变量写在仓库根目录的 `.env`，模板为 `.env.example`：

```bash
cp .env.example .env
```

`app/` 的四个入口（`api`、`trace_api`、`bot`、`cli`）、`python -m ingest`、评测脚本和两个冒烟脚本都经 `services/script_env.py` 的 `load_env()` 读取它，shell 中已设置的变量优先于文件。冒烟脚本和只读数据库的统计脚本可用 `--env-file` 指定其他文件。服务器上的 Docker 部署使用同一个文件，变量传入容器的方式见 [部署与运维](OPERATIONS.md)。

模板只列必填项：

| 变量 | 用途 |
| --- | --- |
| `DB_USER`, `DB_PASS` | 数据库账号。`DB_HOST`、`DB_PORT`、`DB_NAME` 默认为 `127.0.0.1`、`5555`、`DB`，即经 SSH 隧道连接。 |
| `DEEPSEEK_API_KEY` | 取证模型与采集分析。 |
| `VERTEX_PROJECT`, `GOOGLE_APPLICATION_CREDENTIALS` | 综合模型；后者是服务账号 JSON 的本机路径。 |
| `JINA_API_KEY` | 向量、重排与 Reader。 |
| `HTTP_PROXY`, `HTTPS_PROXY` | 本机不能直连 Google、Jina 时填写。 |

可调参数的默认值写在代码里，不进模板；需要修改时在 `.env` 中加一行：

| 变量 | 默认 | 含义 |
| --- | --- | --- |
| `AGENT_GATHER_PROVIDER`, `AGENT_GATHER_MODEL` | `deepseek`, `deepseek-v4-flash` | 取证模型，须支持工具调用。 |
| `AGENT_FINAL_PROVIDER`, `AGENT_FINAL_MODEL` | `vertex`, `gemini-3.1-pro-preview` | 综合模型；评测的判官也用它。 |
| `AGENT_MAX_STEPS`, `AGENT_HISTORY_TURNS`, `AGENT_TEMPERATURE` | `3`, `4`, `0.1` | 取证循环的最大步数（1 到 6）、带入上下文的历史轮数、两个模型的温度。 |
| `INGEST_ANALYZE_PROVIDER`, `INGEST_ANALYZE_MODEL` | `deepseek`, `deepseek-v4-flash` | 采集分析模型。 |
| `INGEST_<字段名大写>` | 见 [部署与运维](OPERATIONS.md) | `IngestConfig` 的任一字段，例如 `INGEST_FETCH_WORKERS`。 |
| `VERTEX_LOCATION` | `global` | Vertex 区域。 |
| `DEEPSEEK_BASE_URL`, `DEEPSEEK_TIMEOUT_SEC` | `https://api.deepseek.com`, `120` | OpenAI 兼容接口的地址与超时秒数。 |
| `GEMINI_API_KEY` | 无 | 某个用途的供应商选 `gemini_api` 时需要。 |
| `NEWS_RERANK_MODE` | `llm_rerank` | 设为 `none` 关闭重排。 |
| `JINA_RERANK_MODEL`, `JINA_RERANK_TIMEOUT_SEC` | `jina-reranker-v3`, `15` | 重排模型与超时秒数。 |
| `RETRIEVAL_POINTS_BOOST`, `RETRIEVAL_FRESHNESS_BOOST`, `RETRIEVAL_TRGM_THRESHOLD` | 关, `0`, `0.42` | 检索排序的热度加权、新鲜度加权，以及精确召回的 pg_trgm 相似度阈值。 |
| `AGENT_TRACE_FULL_MODEL_IO`, `AGENT_TRACE_SECRET_REDACTION` | 开, 开 | Trace 是否记录完整的模型输入输出、是否对疑似凭据的内容脱敏。 |
| `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`, `LANGSMITH_ENDPOINT` | 关 | LangSmith 外部观测；`LANGSMITH_API_KEY` 由 `langsmith` 库读取。 |
| `TRACE_DASHBOARD_TOKEN`, `TRACE_DASHBOARD_ADMIN_EMAIL` | 无, 同 `ADMIN_EMAIL` | 本机运行 Trace Console 时的访问 token 与管理员邮箱。 |
| `BOT_SESSION_MAX_CHATS`, `BOT_SESSION_TTL_SEC`, `BOT_RATE_TRACK_MAX_CHATS`, `BOT_RATE_WINDOW_SEC`, `BOT_RATE_LIMIT`, `BOT_AUTO_DELETE_SEC`, `BOT_SEND_RETRY_ATTEMPTS`, `BOT_SEND_RETRY_BASE_SEC` | 见 `app/bot.py` | Telegram Bot 的会话缓存、限流、消息自动删除和发送重试。 |
| `API_RATE_LIMIT`, `API_RATE_TRACK_MAX_IPS`, `APPROVE_LINK_TTL_SEC`, `SMTP_HOST`, `SMTP_PORT` | 见 `app/rate_limit.py`、`app/security.py`、`services/mail.py` | 接口限流、审批链接有效期、发信服务器。 |

模型变量的规则：每个用途只认自己的 `<用途>_PROVIDER` 和 `<用途>_MODEL`。供应商只认 `gemini_api`、`vertex`、`deepseek` 三个名字；没有给模型名时，取 `services/llm_provider.py` 中 `DEFAULT_MODELS` 的供应商默认值。采集分析只适配 OpenAI 兼容的客户端，供应商换成 `vertex` 或 `gemini_api` 时把 `INGEST_ANALYZE_JSON_MODE` 设为 `false`。向量模型 `jina-embeddings-v3` 写死在 `ingest/embed.py` 与 `agent/retrieval/embeddings.py`，两处必须一致。

## 服务启动

API：

```bash
uvicorn app.api:app --host 0.0.0.0 --port 8000
```

Trace Console API：

```bash
uvicorn app.trace_api:app --host 0.0.0.0 --port 8010
```

Telegram Bot：

```bash
python -m app.bot
```

CLI：

```bash
python -m app.cli
```

MCP stdio server：

```bash
python -m agent.mcp.stdio_server
```

采集管线：

```bash
python -m ingest sync-sources        # 把 ingest/sources.toml 同步到数据库
python -m ingest run                 # 执行一轮
python -m ingest run --stage fetch   # 只执行一个阶段
python -m ingest status              # 积压与近 24 小时失败
python -m ingest loop                # 常驻运行，每轮间隔 300 秒
```

本机连接的是 `.env` 中配置的数据库；`run`、`loop` 会写库并调用 Reader、模型和向量接口。同一个数据库上只有一个采集进程处理文章，已有进程运行时 `run` 退出。

冒烟脚本，调用真实模型，由使用者运行并查看输出：

```bash
# 取证模型与综合模型各一次调用，不连数据库
python deployment/scripts/smoke_agent.py

# 取旧表中的 20 篇文章执行 analyze，只读数据库，不写入
python deployment/scripts/smoke_analyze.py
```

## Trace Dashboard

开发模式：

```bash
cd trace_dashboard
npm install
npm run dev
```

生产构建：

```bash
cd trace_dashboard
npm run build
```

预览构建产物：

```bash
cd trace_dashboard
npm run preview
```

生产环境由 `trace` 容器提供静态页面和 `/trace-api/*` 接口。

## 测试

单元测试：

```bash
pytest tests/unit -q
```

测试不读取开发者的 `.env`：`tests/conftest.py` 把 `services.script_env.load_env` 替换为空操作。

重点模块测试：

```bash
pytest tests/unit/test_graph_turn.py tests/unit/test_graph_parallel.py -q
pytest tests/unit/test_tools_registry.py tests/unit/test_read_articles.py tests/unit/test_evidence_registry.py -q
pytest tests/unit/test_citations.py tests/unit/test_context.py -q
pytest tests/unit/test_search_stories.py tests/unit/test_stories.py tests/unit/test_rerank_flow.py -q
pytest tests/unit/test_agent_trace.py tests/unit/test_agent_trace_store.py -q
pytest tests/unit/test_ingest_store.py tests/unit/test_ingest_stories.py tests/unit/test_ingest_monitor.py -q
```

采集解析层的测试样本在 `tests/fixtures/ingest/`。

Python 编译检查：

```bash
python -m py_compile \
  app/api.py app/trace_api.py app/bot.py \
  agent/turn.py agent/graph/nodes.py agent/trace.py \
  ingest/runner.py ingest/store.py \
  services/agent_trace_store.py
```

编码检查：

```bash
python eval/encoding_guard.py --root . --report eval/reports/encoding_guard/local.json
```

## CI

CI 工作流位于 `.github/workflows/ci.yml`。

触发范围：

- `agent/**`
- `app/**`
- `services/**`
- `ingest/**`
- `eval/**`
- `tests/**`
- `requirements.txt`
- `constraints.txt`
- `.github/workflows/ci.yml`

CI 步骤：

1. 安装 Python 3.12。
2. 安装 `requirements.txt`（带 `-c constraints.txt`）和 `pytest`。
3. 运行编码检查，失败不阻断。
4. 执行 `pytest tests -v`。
5. 上传编码检查报告。

## 目录结构

| 路径 | 说明 |
| --- | --- |
| `ingest/` | 采集管线：来源定义 `sources.toml`、六个处理阶段、监控、旧表回填与重标。 |
| `agent/` | Agent 运行时：`turn.py`（入口 `run_turn`）、`graph/`（编排）、`tools/`（两个工具）、`retrieval/`（检索）、证据登记表、引用校验、线程上下文、Trace、MCP。 |
| `app/` | API、Trace API、Telegram Bot、CLI。 |
| `services/` | DB、对话线程、Trace Store、邮件、模型工厂、地址归一、领域与类型词表。 |
| `trace_dashboard/` | Vue3 Trace Console。 |
| `frontend/` | 静态落地页与订阅页面。 |
| `eval/` | 评测与验收脚本、评分、报告生成、Trace 查询；`legacy/` 存放归档的评测。 |
| `tests/unit/` | 单元测试。 |
| `tests/fixtures/ingest/` | 采集解析层的录制样本。 |
| `sql/infrastructure/` | schema、view、seed、数据质量检查 SQL。 |
| `sql/analytics/` | Metabase 和分析查询 SQL。 |
| `docker-compose.yml`、`.env.example` | 仓库根目录的 Compose 配置与环境变量模板。 |
| `deployment/` | 数据库运维脚本、冒烟脚本。 |
| `etl_workflow/` | 旧采集链路的 n8n 工作流 JSON，仅作存档。 |
| `docs/` | 项目长期文档。 |

## 代码边界

- 业务入口在 `app/`。
- Agent 编排、工具和检索在 `agent/`，LangGraph 只在 `agent/graph/` 中导入；检索、工具、证据登记表和引用校验不依赖图。
- 采集管线在 `ingest/`：解析和判定逻辑是不访问网络与数据库的纯函数，HTTP 请求经 `ingest/http.py`，模型调用在 `ingest/analyze.py`，SQL 集中在 `ingest/store.py`。
- 可复用基础服务在 `services/`。
- `news` schema 的结构变更写入 `sql/infrastructure/schema/news_schema.sql`，其余表的结构变更写入 `sql/infrastructure/schema/schema_ddl.sql`。
- 部署和数据库运维脚本放入 `deployment/scripts/`。
- 评测逻辑放入 `eval/`，测试放入 `tests/unit/`。
- 运行产物不作为源码维护，包括 `eval/reports/*`、临时数据集、缓存、构建产物和虚拟环境。
