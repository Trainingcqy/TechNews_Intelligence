# 评测体系

评测分两部分：数据层评测检查采集管线和派生数据（分类、故事、来源覆盖），Agent 评测检查检索、生成和问答链路。Agent 评测复用线上链路，定位并量化每一层的失效。系统架构见 [系统架构](ARCHITECTURE.md)。

各项评测的结果由脚本写入各自目录的 `report.md` 或标准输出，本文只写方法和运行步骤。

---

## 1. 评测对象与目标

被评测对象有两个：

- **数据层**：采集管线 `ingest/` 写入 `news` schema 的数据，包括文章的状态与失败、领域与类型标签、故事归并、来源覆盖。
- **Agent**：`agent.run_turn` 的问答链路。它是一张 LangGraph StateGraph：`prepare → gather ⇄ run_tool → collect → route → synthesize → finalize`；取证模型调用 `search_news`、`read_articles` 两个工具，综合模型依据证据登记表中的证据作答，代码校验引用并生成来源列表。回答必须由工具证据支撑。

评测目标不是"答案读起来好不好"，而是用数据定位并量化系统在每一层会在哪里出问题：数据层看管线失败率、标签质量、归并阈值和来源覆盖；Agent 层看检索排序、回答的忠实度、无证据时的拒答，以及验收问题上的行为和耗时。每项评测用独立构造的题集或标注、复用线上链路、可经 Trace 回溯，产出少量可辩护的指标。

---

## 2. 设计原则

- **题集独立于被测系统**：题目手写，不由被测模型生成，避免"同源模型出题又评分"的指标自证。
- **复用线上链路**：生成评测和验收问题集直接调用 `agent.run_turn`，检索评测调用 `agent.retrieval.search.lookup_candidates_by_query`，不维护 mock 链路，评的就是线上行为。
- **隔离**：每条 case 用空 history、独立 `thread_id`、独立 `request_id`，避免上下文串台。
- **结构化优先，LLM-as-judge 兜底**：能用确定性信号（引用地址是否出现在正文、标准答案是否出现、标签比例）就不用 LLM 打分；开放文本质量才用 LLM judge，并对 judge 做人机一致性校准（kappa）。判官默认取综合模型（`AGENT_FINAL_*`）。
- **标注表**：需要人工判断的统计（故事对、故事级检查、分类抽检、转译占比、检索相关性校准）都生成 Markdown 标注表，在方括号内填写答案，由脚本读回统计。故事对和检索相关性校准的标注表不显示机器结果（盲标），答案另存文件。
- **可回溯**：每个失败 case 都能经 `agent_model_io`、`agent_trace_spans` 还原到取证、工具、检索、综合哪一层。
- **断点恢复**：长跑脚本按 JSONL append 记录，可中断续跑（429、宕机不丢进度）；改动被测代码后须先清空对应的断点文件才会重测，见 §7「强制重跑」。
- **限流退避**：调用模型的脚本遇到 429 时指数退避（共享 `eval/eval_retry.py`），`--sleep-seconds` 拉大用例间隔。
- **数据冻结**：G2、G3 运行前暂停采集（`docker compose stop ingest`），使各配置、各题在同一份数据库快照上比较；两个脚本启动时要求确认，`--skip-confirm` 跳过。
- **只读与分工**：数据层的统计脚本对数据库只读；需要调用模型的脚本由使用者运行并观察输出。

---

## 3. 评测清单

**数据层**

| 评测 | 位置 | 核心问题 | 核心指标 |
|---|---|---|---|
| 分类试标 | `eval/taxonomy/` | 领域与类型词表能否稳定标注？ | 实验室与模型名文章的 `ai` 标签比例、同事件文章对的一致率、人工抽检正确率、各键占比 |
| 故事归并阈值校准 | `eval/stories/` | 阈值取多少？故事是否是单一事件？ | 各阈值的精确率与召回率、故事跨度与成员数分布 |
| 来源覆盖 | `eval/coverage/` | 语料覆盖了多少当日头条？ | 地址命中率、事件命中率、缺失媒体、头条的排名 |
| 跨来源重叠与转译占比 | `eval/stories/cross_source.py` | 多发布方报道的故事占多少？中文报道是否多为转译？ | 重叠率、转译占比 |

**Agent**

| 层 | 位置 | 核心问题 | 核心指标 |
|---|---|---|---|
| **G2 检索质量** | `eval/retrieval/` | 检索召回排序好不好？重排值不值？ | nDCG@10 / MRR@10 / Hit@5 |
| **G3 生成忠实度** | `eval/generation/` | 答案是否被证据支撑、是否幻觉？ | Faithfulness(1-5) / 幻觉率 / 地址泄漏 |
| **G6 RGB 拒答** | `eval/rgb/` | 只给干扰文档时会不会冒充证据？ | 误归因率 |
| **验收问题集** | `eval/acceptance/` | 真实问题上，工具使用、引用、角色归属是否符合预期？ | 逐条人工判断；状态分布、步数、耗时、token |
| **效率** | `eval/trace_query.py`、`eval/retrieval/sql_latency.py` | 各节点耗时与并行收益；混合检索的 SQL 耗时随时间窗的变化 | 节点耗时与 token 的中位数；并行收益；`days=14`、`days=365` 的 SQL 耗时中位数 |
| **追踪型验收集** | `eval/tracking/` | 盘点、追踪、对照三类任务是否达标？ | 召回率、完整率、顺序、"自上次以来"、角色覆盖；归属与评论表述由人工判断 |

---

## 4. 评测流程

Agent 评测遵循同一条 5 步流水线；差异只在"造什么题、捕获什么、怎么打分"。

1. **造题**：手写独立于被测系统的题集。G2 标检索时间窗，G3 为开放问答加对抗性诱饵，G6 用公开基准 RGB 的中文子集，验收问题集覆盖问答、盘点、追踪、对照四类任务。
2. **跑真实链路**：G3 和验收问题集调用 `run_turn`；G2 调用检索函数，在三个消融配置下各取 top-10；G6 把只含干扰文档的证据块交给生产综合提示词。
3. **捕获中间产物**：落盘每步可判信号：检索排序、答案、回答状态、工具调用序列、来源集合；G3 额外从 Trace（`agent_model_io`）取回综合模型真正看到的证据。
4. **打分（结构化优先）**：地址是否出现在正文、标准答案是否出现等用确定性信号；检索相关性和生成忠实度用 LLM-as-judge，并对 judge 做人机一致性（kappa）校准。
5. **出报告 + 归因**：自动生成指标表；失败可回溯到取证、工具、检索、综合具体哪一层。

---

## 5. 各项详解

### 5.1 数据层

数据层的脚本默认从仓库根目录的 `.env` 读取数据库配置，可用 `--env-file` 指定其他文件；对数据库只读。

**分类试标**（`eval/taxonomy/build_sample.py`、`score.py`，标注用 `python -m ingest relabel`）：

1. `build_sample.py` 抽样：旧分类每类 50 篇；另从标题含 AI 实验室或模型名的文章中随机抽 200 篇；另取全部相似度不低于 0.94 且相隔不超过 3 天的文章对。输出 `sample.json` 与 `sample_keys.txt`（每行一个 `url_key`）。
2. `python -m ingest relabel --keys-file sample_keys.txt` 标注这些文章。
3. `score.py sheet` 生成 100 篇的人工抽检表，`score.py report` 统计下表指标。

| 指标 | 通过条件 |
| --- | --- |
| 标题含 AI 实验室或模型名的文章中带 `ai` 标签的比例 | 不低于 95% |
| 同事件文章对的主领域一致率 | 不低于 90% |
| 同事件文章对的类型一致率 | 记录数值；低于 80% 时重写类型的范围描述 |
| 人工抽检 100 篇的主领域正确率、类型正确率 | 分别不低于 90%、85% |
| 各键的占比 | `general` 低于 15%；占比低于 2% 的键需要复核 |

`score.py` 只统计 `label_source = 'model'` 且 `topics` 非空的文章，同事件文章对只计两篇都已重标的对。结果同时用于决定是否对旧文章全量重标。

**故事归并阈值校准**（`eval/stories/build_label_sheet.py`、`compute_threshold.py`、`story_spans.py`）：

1. `build_label_sheet.py` 从 `news.story_member` 按 `nearest_similarity` 分段抽样文章对，默认每 0.02 一段，同语言每段 20 对，跨语言文章对另抽 40 对；输出不显示相似度的标注表和记录答案的 key 文件。
2. 人工标注每对是否同一事件；`compute_threshold.py` 列出 0.75 到 0.95、步长 0.01 的各阈值的精确率、召回率和 F1，召回率按各段的总体数量加权，阈值由人确定。
3. 按对标注测不出串联，所以 `story_spans.py report` 统计故事跨度与成员数的分布，并生成成员最多、跨度最长的故事的抽查表，`story_spans.py score` 统计其中包含多个事件的比例。

**来源覆盖**（`eval/coverage/techmeme_coverage.py`）：参照渠道积累满 7 天后执行。

| 指标 | 计算 |
| --- | --- |
| 地址命中率 | `reference_headline.target_url_key` 出现在 `news.article.url_key` 中的比例 |
| 事件命中率 | 地址未命中的头条：对标题生成查询向量，取事件时间在前后 2 天内最相近的 3 个故事作为候选，人工判断是否报道了同一事件 |
| 缺失媒体 | 未命中的头条按 Techmeme 标注的媒体名计数 |
| 头条的排名 | 命中的头条所属故事在当天按重要性排序中的名次；对候选排序规则分别计算"进入当天前 20 名的比例" |

`report` 子命令统计地址命中率、缺失媒体和排名；`event-sheet` 生成事件命中的标注表（需要 `JINA_API_KEY` 生成查询向量）；`event-score` 统计事件命中率。Techmeme 是英文头条，很少收录国内事件，以它为参照的结论只对英文事件成立。

**跨来源重叠率与转译占比**（`eval/stories/cross_source.py`）：

- `overlap`：可检索文章中属于多发布方故事的比例，及分渠道的重叠率；窗口内不少于 20 篇而重叠率为 0 的渠道列为复核对象。
- `sheet`、`score`：从中英文都有的故事中抽 50 个，每个故事随机取一篇中文、一篇英文，人工判断中文一篇是否只是英文报道的翻译或改写，统计转译占比。占比超过一半时，`publisher_count` 应改为按语言分别计数后取较大值。

### 5.2 Agent

#### G2 · 检索质量 + 矩阵 ablation

- **方法**：覆盖良好的 entity×aspect query；对 3 个配置（G0 基础召回 + 不重排 / G1 宽召回 + 不重排 / G2 宽召回 + Jina 重排）的 top-10 做 **TREC pooling**，用 LLM judge 打 0/1/2 相关性作为共享 gold；计算 IR 指标（nDCG 用指数增益 2^rel−1）。召回配置经环境变量 `EVAL_RECALL_PROFILE` 切换（`base`、`wide`）。
- **判官**：取综合模型（`AGENT_FINAL_*`）。对判官做人机一致性（Cohen's kappa）校准：盲标、隐藏 LLM 分以避免锚定；有序标签看 quadratic-weighted kappa，二元相关性 kappa 对应 Hit/MRR/P 实际依赖的"相关/不相关"判断。
- **精确召回**：`queries_exact.jsonl` 是 40 条精确匹配类问题（实体名、模型名、中英文对照、罕见词各 10 条），用于检查精确召回路（实体别名与 pg_trgm），结果在 `report_exact.md`。
- **文件**：`queries.jsonl` · `queries_exact.jsonl` · `run_retrieval.py` · `judge_relevance.py` · `compute_metrics.py` · `build_calibration_sheet.py`（盲标、隐藏 key）· `compute_kappa.py` · `report.md` · `calibration_report.md`

#### G3 · 生成忠实度（+ 对抗）

- **方法**：对每个问题调用 `run_turn`，按回答状态分桶：`answered` 记 `success`，`direct` 记 `clarification`，`insufficient`、`not_found`、`blocked` 记 `refusal`，`error` 记 `error`。只有 `success` 进入忠实度评判。
- **证据对齐**：`enrich_evidence.py` 从 `agent_model_io`（节点名 `final_synthesizer`）取回**综合模型真正看到的证据块**，judge 的评分依据是这份 `synthesizer_evidence`，而非从数据库重抓的摘要，保证评的是模型实际所见。
- **打分**：LLM judge 打 faithfulness / answer_relevancy（1-5）；确定性 `url_leak`：回答正文中不应出现任何地址，来源由后端渲染。
- **对抗集（幻觉诱饵）**：用"不存在的型号 / 精确数字 / 完整名单"等诱饵压测诚实性。
- **提示词 A/B**：`run_generation.py --synth-prompt-file` 用指定文件替换 `agent.prompts.FINAL_SYSTEM_PROMPT`，量化提示词的边际贡献；替换后的提示词仍须要求输出 `STATUS` 首行并使用 `[E…]` 证据编号引用，否则回答被引用校验拦截。
- **文件**：`queries.jsonl` · `queries_adversarial.jsonl` · `run_generation.py` · `enrich_evidence.py` · `judge_faithfulness.py` · `report.md` · `report_adversarial.md`

#### G6 · RGB negative-rejection（对抗性拒答）

- **方法**：取 RGB 中文子集，每条只喂 `negative`（不含答案）文档 top-5，渲染成证据块（编号 `[E1]` 到 `[E5]`，标题写作"RGB 文档 N"），经生产接地提示词合成答案，压测"无证据时会不会冒充出处"。输出首行为 `STATUS: insufficient` 的直接计为拒答，不调用判官；`STATUS: answered` 的交给 LLM judge。
- **指标三分**：`misattributed`（把证据外内容冒充成证据支撑 = 真·幻觉，标注假设的不算）是核心失败量；`gold_present`（确定性字串匹配，数字归一）、`answered`（LLM judge）用于区分"干净拒答 / 标注假设补充 / 冒充出处"三种行为。
- **模型与判官**：合成模型默认取综合模型（`AGENT_FINAL_*`），`--provider`、`--model` 可覆盖；只给 `--provider` 且与综合模型的供应商不同时，取该供应商的默认模型。`--judge-provider`、`--judge-model` 另指判官，避免自评，只给 `--judge-provider` 且与合成模型的供应商不同时同样取该供应商的默认模型；不指定判官时与合成模型相同。`--synth-prompt-file` 覆盖综合提示词做 A/B。
- **文件**：`run_rgb_rejection.py` · `data/zh.json` · `report.md`

#### 验收问题集

- **问题集**：`eval/acceptance/questions.jsonl`，30 到 50 条实际会问的问题，由使用者手写，四类任务（问答、盘点、追踪、对照）都要覆盖。每行一个 JSON 对象：`question` 必填；`case_id` 选填，默认 `q<行号>`，不超过 40 字符（`request_id` 是 `VARCHAR(64)`）；`task` 选填，取问答、盘点、追踪、对照之一，只回显到表里。
- **运行**：`run_acceptance.py` 对每条调用 `run_turn`，记录状态、工具调用序列、步数、来源数、耗时、token：`run_turn` 返回后只读查询 Trace 表，步数是 `collect` span 的个数，token 取 `agent_runs.token_usage`，工具参数取 `tool_call` span 的 `input_summary.args`。输出 `runs/acceptance.jsonl` 和 `report.md`，可断点续跑，`error` 的条目续跑时重跑。
- **人工判断**：`report.md` 的表中留空五列，逐条判断：该调用工具的问题是否调用了工具（统计 `direct` 状态下的事实性问题数）；多对象问题是否每个对象各检索了一次；没有具体对象的问题是否走了按重要性的浏览；回答里的引用是否支持对应的句子（抽查引用精确率）；官方说法和社区评论是否按综合提示词的 Source Roles 归属。
- **已知风险**：取证模型可能不调用工具就回答事实问题。代码无法从语义上识别，由提示词约束，在验收问题集上统计发生次数。

#### 追踪型验收集

- **验收集**：`eval/tracking/` 下的 `inventory.jsonl`（盘点）、`tracking.jsonl`（追踪）、`contrast.jsonl`（对照），各 10 条，由使用者依据库内数据手工构造。每行一个 JSON 对象，`case_id` 必填，不超过 40 字符。盘点给 `question` 与 `expected_story_ids`，即人工筛到该领域的 Techmeme 头条在库内对应的故事；追踪给 `question` 与按时间排列的 `progress`（每项 `article_id`、`label`），可选的 `follow_query`、`checked_at`、`followup_question` 三项一起给出时做"自上次以来"的检查；对照给 `question` 与 `story_id`。
- **运行**：`run_tracking.py run` 对每条用空历史调用 `run_turn`，把回答的来源按 `url_key` 对回库内文章，讨论按讨论页地址对回被讨论文章的故事。"自上次以来"的检查用评测专用的 `user_key` `eval:tracking` 建一个关注项，查看时间设为 `checked_at` 的北京零点，带这个 `user_key` 提问，前后都删除这个 `user_key` 的关注项；这是脚本唯一的写库操作。输出 `runs/<验收集>.jsonl` 与 `report.md`，可断点续跑，`error` 的用例续跑时重跑。
- **指标**：盘点的召回率是期望故事中被引用来源覆盖的比例，非事件类条目数是被引用文章中 `tutorial`、`opinion`、`promo` 的篇数，列表行都要带 `[n]`。追踪的完整率是有同故事来源被引用的进展比例；顺序要求被覆盖的进展在正文中首次被引用的位置按时间递增；"自上次以来"要求被引用的进展来源中没有早于 `checked_at` 的。对照的三个角色只看目标故事内的被引用来源，要同时有官方发布方、媒体和讨论。召回率与完整率按全部条目汇总，其余按用例计。
- **人工判断**：对照组的归属是否正确、是否把评论表述为事实，填在 `contrast_sheet.md` 中，`y` 表示有问题；`run_tracking.py score` 统计后刷新 `report.md`，重跑时保留已填的判断。
- **通过条件**：召回率不低于 70%，非事件类条目为 0，全部带引用；完整率不低于 80%，顺序全部正确，"自上次以来"不混入旧进展；角色覆盖不低于 90%，归属错误为 0，评论被表述为事实为 0。

#### 效率

`eval/retrieval/sql_latency.py` 测混合检索的 SQL 耗时。向量在时间窗内精确排序，不建近似索引；`days=365` 的中位数超过 1 秒时改用近似索引。检索词取 `queries.jsonl` 的前 20 条，SQL 与参数由 `fetch_hybrid_rows` 按默认参数的 `search_news` 生成，在 `EXPLAIN (ANALYZE, TIMING OFF)` 下执行，耗时取数据库报告的规划与执行时间之和，不含网络往返。只读；每条检索词请求一次向量。

`eval/trace_query.py --node-stats --since <日期>` 按节点统计耗时和 token 的中位数，并与脚本中记录的旧链路中位数对比。起点按北京时间零点；统计 `graph_node`、`model_call`、`tool_call` 三类 span，不筛状态；`gather` 对应旧链路的工具规划，旧链路的意图判断在当前链路没有对应节点。同一输出还统计一步内多个调用时 `run_tool` 各分支耗时之和与该步实际耗时之比，作为并行收益的测量。

---

## 6. 归档的评测

旧链路的评测与结果保留在 tag `v1-n8n`：错误分析、意图分类、工具选择、上下文预算四项评测，以及 G2、G3、G6 在旧链路上的运行记录和报告。它们针对的意图分类器、工具集和上下文预算在当前链路中不存在。

---

## 7. 完整评测流程

各入口脚本相互独立、可单独运行，调用模型的脚本建议手动运行并观察输出。G2、G3 运行前暂停采集，脚本会弹确认，加 `--skip-confirm` 跳过；G6 用自带的 `data/zh.json`，不触新闻库，无需暂停。

### 强制重跑（改代码后必读）

断点续跑的代价：每个 LLM / agent 脚本把对应的 `runs/*.jsonl` 当作续跑状态，按 `status` 跳过已完成的条目。**当所有用例都已完成后，直接重跑不会重新测试**，脚本跳过全部、仅用旧缓存重建报告。所以改动被测代码后要看真实新结果，**必须先清空对应评测的断点文件**。

| 阶段 | 断点文件（清它才会重跑） |
|---|---|
| G2 检索 | `eval/retrieval/runs/retrieval_results.jsonl` |
| G2 相关性评判（LLM judge） | `eval/retrieval/runs/relevance_judgments.jsonl` |
| G3 生成 | `eval/generation/runs/generation.jsonl` |
| G3 忠实度评判（LLM judge） | `eval/generation/runs/faithfulness_judgments.jsonl` |
| G6 RGB 拒答 | `eval/rgb/runs/rgb_rejection.jsonl`（或对应 `--output`） |
| 验收问题集 | `eval/acceptance/runs/acceptance.jsonl`（`error` 的条目续跑时自动重跑） |

- **G3 是多阶段链**（run → enrich → judge）：重测某一集需清掉该集的 run 与 judge 两个 `runs/*.jsonl`，再依次重跑三步；对抗集对应各自 `--output` 文件名。
- **纯计算 / 转换阶段**（`compute_metrics` / `compute_kappa` / `enrich_evidence` / `build_*` / `score`）每次都从输入重算，无续跑、无需清理。
- `--report-only`（`judge_faithfulness.py`、`run_rgb_rejection.py` 提供）是反向操作：完全不跑、只用现有断点重建报告。

强制重跑示例（以 G6 为例，保留旧结果以便对比）：

```powershell
Move-Item eval\rgb\runs\rgb_rejection.jsonl eval\rgb\runs\rgb_rejection.before.jsonl
Copy-Item eval\rgb\report.md eval\rgb\report.before.md
python eval/rgb/run_rgb_rejection.py --limit 0
```

> 判读提醒：LLM 阶段（judge、综合模型）有模型不确定性，清断点重跑出现 ±1~2 条波动属正常，不等于回归。若改的是确定性代码，单元测试才是权威回归闸门，无需为验证它重跑 LLM 阶段。

### 数据层

```bash
# 分类试标
python eval/taxonomy/build_sample.py                                   # -> runs/sample.json、runs/sample_keys.txt
python -m ingest relabel --keys-file eval/taxonomy/runs/sample_keys.txt
python eval/taxonomy/score.py sheet                                    # -> runs/spot_check.md（人工抽检表）
python eval/taxonomy/score.py report --sheet eval/taxonomy/runs/spot_check.md

# 故事归并阈值校准
python eval/stories/build_label_sheet.py                               # -> pair_sheet.md + pair_key.json
python eval/stories/compute_threshold.py
python eval/stories/story_spans.py report                              # -> story_sheet.md
python eval/stories/story_spans.py score

# 来源覆盖（参照渠道积累满 7 天后）
python eval/coverage/techmeme_coverage.py report
python eval/coverage/techmeme_coverage.py event-sheet                  # 需要 JINA_API_KEY
python eval/coverage/techmeme_coverage.py event-score

# 跨来源重叠率与转译占比
python eval/stories/cross_source.py overlap --days 14
python eval/stories/cross_source.py sheet
python eval/stories/cross_source.py score
```

### G2 · 检索质量 + kappa 校准

```powershell
python eval/retrieval/run_retrieval.py --skip-confirm                  # -> runs/retrieval_results.jsonl（G0/G1/G2 三配置 top-10 pooling）
python eval/retrieval/judge_relevance.py                               # -> runs/relevance_judgments.jsonl（LLM 打 0/1/2 共享 gold）
python eval/retrieval/compute_metrics.py                               # -> report.md（nDCG/MRR/Hit/P@5 + ablation delta）
# judge 可信度校准（人机一致性）
python eval/retrieval/build_calibration_sheet.py                       # -> calibration_sheet.md（盲标）+ runs/calibration_key.jsonl（隐藏 LLM 分）
python eval/retrieval/compute_kappa.py                                 # -> calibration_report.md（Cohen's kappa + 混淆矩阵）
```

### G3 · 生成忠实度（标准 / 对抗）

```powershell
# 标准集
python eval/generation/run_generation.py --output eval/generation/runs/generation.jsonl --skip-confirm
python eval/generation/enrich_evidence.py --results eval/generation/runs/generation.jsonl --output eval/generation/runs/generation_enriched.jsonl
python eval/generation/judge_faithfulness.py --results eval/generation/runs/generation_enriched.jsonl --output eval/generation/runs/faithfulness_judgments.jsonl --report eval/generation/report.md

# 对抗集（幻觉诱饵）
python eval/generation/run_generation.py --queries eval/generation/queries_adversarial.jsonl --output eval/generation/runs/generation_adversarial.jsonl --skip-confirm
python eval/generation/enrich_evidence.py --results eval/generation/runs/generation_adversarial.jsonl --output eval/generation/runs/generation_adversarial_enriched.jsonl
python eval/generation/judge_faithfulness.py --results eval/generation/runs/generation_adversarial_enriched.jsonl --output eval/generation/runs/faithfulness_adversarial.jsonl --report eval/generation/report_adversarial.md
```

### G6 · RGB 拒答（用自带 `data/zh.json`，不触新闻库，无需暂停采集）

```powershell
# 生产综合模型与生产提示词
python eval/rgb/run_rgb_rejection.py --limit 0
# 指定合成模型与另外的判官，避免自评；只给供应商时取其默认模型，要指定模型名时再加 --model、--judge-model
python eval/rgb/run_rgb_rejection.py --limit 0 --provider deepseek --model deepseek-flash --judge-provider vertex --judge-model gemini-3.1-pro-preview --output eval/rgb/runs/rgb_deepseek.jsonl --report eval/rgb/runs/rgb_deepseek.md
```

### 验收问题集

```powershell
python eval/acceptance/run_acceptance.py                               # -> runs/acceptance.jsonl + report.md
python eval/trace_query.py --node-stats --since 2026-11-01             # 节点耗时与并行收益；日期为示例，按实际起点填写
python eval/retrieval/sql_latency.py                                   # 混合检索 SQL 耗时：days=14 与 365 各 20 条
```

### 追踪型验收集

```powershell
python eval/tracking/run_tracking.py run --set inventory --only-case-id i01   # 冒烟：只跑一条盘点用例
python eval/tracking/run_tracking.py run                                      # 三组全部；-> runs/*.jsonl + report.md + contrast_sheet.md
python eval/tracking/run_tracking.py score                                    # 填完 contrast_sheet.md 后统计人工判断
```
