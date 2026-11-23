<script setup>
import { computed } from "vue";
import { compactArgs, formatDate, formatMs, formatScore, formatTime } from "../format";
import { REASON } from "../labels";
import { articleTitle, candidateRows, durationMs, evidenceTitle, nodeTitle, parentOf, rerankScores } from "../trace";
import WarningList from "./WarningList.vue";

const props = defineProps({
  trace: { type: Object, required: true },
  node: { type: Object, required: true }
});

const output = computed(() => props.node.output || {});
const isSearch = computed(() => props.node.kind === "tool" && props.node.name === "search_news");
const isRead = computed(() => props.node.kind === "tool" && props.node.name === "read_articles");
const isRetrievalStage = computed(() => props.node.kind === "retrieval" || props.node.kind === "rerank");

const args = computed(() =>
  Object.entries(props.node.input || {}).filter(
    ([, value]) => value !== null && value !== undefined && !(Array.isArray(value) && !value.length)
  )
);

const stories = computed(() => {
  if (!isSearch.value) return [];
  const scores = rerankScores(props.node);
  return (output.value.stories || []).map((story) => ({
    ...story,
    eid: props.trace.evidenceByArticle.get(story.lead)?.eid || "",
    title: articleTitle(props.trace, story.lead),
    meta: articleMeta(story.lead),
    rerank: scores.get(story.lead)
  }));
});

const readItems = computed(() =>
  isRead.value
    ? (output.value.items || []).map((item) => ({ ...item, label: itemLabel(item), title: itemTitle(item) }))
    : []
);

const historyEvidence = computed(() => {
  const rows = props.trace.run.output?.evidence || [];
  return (output.value.history_evidence || []).map((eid) => {
    const row = rows.find((item) => item.eid === eid);
    return { eid, title: row ? evidenceTitle(props.trace, row) : "" };
  });
});

const searchTool = computed(() => (isRetrievalStage.value ? parentOf(props.trace, props.node) : null));
const candidates = computed(() => (searchTool.value ? candidateRows(props.trace, searchTool.value) : []));
const retrieval = computed(() => searchTool.value?.children.find((child) => child.kind === "retrieval") || null);

function articleMeta(articleId) {
  const article = props.trace.articles[String(articleId)];
  return article ? `${article.publisher} · ${formatDate(article.event_at)}` : "";
}

function itemLabel(item) {
  if (item.sighting_id != null) return props.trace.evidenceBySighting.get(item.sighting_id)?.eid || item.target;
  if (item.article_id != null) return props.trace.evidenceByArticle.get(item.article_id)?.eid || item.target;
  return item.target;
}

function itemTitle(item) {
  if (item.sighting_id != null) {
    const discussion = props.trace.discussions[String(item.sighting_id)];
    return discussion ? `HackerNews 讨论：${discussion.title}` : "";
  }
  return item.article_id != null ? articleTitle(props.trace, item.article_id) : "";
}

function filterText(input) {
  return [input.topic, ...(input.kinds || []), input.publisher_kind].filter(Boolean).join("、") || "无";
}
</script>

<template>
  <div class="detail">
    <header class="detail-head">
      <h3>{{ nodeTitle(node) }}</h3>
      <div class="chips">
        <span v-if="node.status === 'error'" class="status status-error">出错</span>
        <span v-else-if="node.status === 'aborted'" class="status status-error">中断</span>
        <span>{{ formatMs(durationMs(node)) }}</span>
        <span v-if="node.attributes?.call_id" class="mono muted">{{ node.attributes.call_id }}</span>
      </div>
      <WarningList v-if="node.warnings.length" :items="node.warnings.map((code) => ({ code, node }))" />
      <template v-if="node.error_code">
        <p class="error-text">{{ node.error_code }} {{ node.error_message }}</p>
        <details v-if="node.exception" class="fold">
          <summary><span class="fold-title">回溯</span></summary>
          <pre class="code">{{ node.exception }}</pre>
        </details>
      </template>
    </header>

    <template v-if="node.kind === 'context'">
      <dl class="kv">
        <dt>历史</dt>
        <dd>带入 {{ output.turns_used ?? 0 }} 轮，共 {{ output.history_turns ?? 0 }} 轮</dd>
        <dt>链接</dt>
        <dd>问题与历史用户消息中 {{ output.user_urls ?? 0 }} 个</dd>
      </dl>
      <section class="block">
        <h4>由历史来源登记的证据</h4>
        <ul v-if="historyEvidence.length" class="plain-list">
          <li v-for="row in historyEvidence" :key="row.eid"><span class="mono">{{ row.eid }}</span> {{ row.title }}</li>
        </ul>
        <p v-else class="muted">无</p>
      </section>
      <section class="block">
        <h4>关注项</h4>
        <ul v-if="output.follows?.length" class="plain-list">
          <li v-for="follow in output.follows" :key="follow.query">
            {{ follow.query }}
            <span class="muted">{{ follow.last_checked_at ? `上次查看 ${formatDate(follow.last_checked_at)}` : "尚未查看" }}</span>
          </li>
        </ul>
        <p v-else class="muted">无</p>
      </section>
    </template>

    <template v-else-if="node.kind === 'step'">
      <section v-if="output.calls?.length" class="block">
        <h4>模型提出的调用</h4>
        <div class="table-wrap">
          <table class="grid-table">
            <thead>
              <tr><th>#</th><th>工具</th><th>参数</th><th>处置</th></tr>
            </thead>
            <tbody>
              <tr v-for="call in output.calls" :key="call.index">
                <td class="mono">{{ call.index }}</td>
                <td class="mono">{{ call.name }}</td>
                <td class="mono small">{{ compactArgs(call.args) }}</td>
                <td>
                  <template v-if="call.disposition === 'executed'">执行</template>
                  <template v-else>
                    <div class="error-text">拒绝：{{ REASON[call.reason] || call.reason }}</div>
                    <div class="muted small">{{ call.text }}</div>
                  </template>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>

    <template v-else-if="node.kind === 'tool'">
      <dl class="kv">
        <template v-for="[key, value] in args" :key="key">
          <dt class="mono">{{ key }}</dt>
          <dd>{{ Array.isArray(value) ? value.join("、") : value }}</dd>
        </template>
      </dl>
      <section v-if="isSearch" class="block">
        <h4>命中的故事 <span class="muted">{{ stories.length }} 个</span></h4>
        <div v-if="stories.length" class="table-wrap">
          <table class="grid-table">
            <thead>
              <tr><th>编号</th><th>标题</th><th>报道</th><th>匹配度</th><th>重排分</th></tr>
            </thead>
            <tbody>
              <tr v-for="story in stories" :key="story.story_id">
                <td class="mono">{{ story.eid }}</td>
                <td>
                  <div>{{ story.title }}</div>
                  <div class="muted small">{{ story.meta }}</div>
                </td>
                <td>{{ story.reports }} 篇 · {{ story.publishers }} 家</td>
                <td class="mono">{{ formatScore(story.match_score) }}</td>
                <td class="mono">{{ formatScore(story.rerank) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
      <section v-if="isRead" class="block">
        <h4>读取结果</h4>
        <div class="table-wrap">
          <table class="grid-table">
            <thead>
              <tr><th>目标</th><th>标题</th><th>读取</th></tr>
            </thead>
            <tbody>
              <tr v-for="item in readItems" :key="item.target">
                <td class="mono">{{ item.label }}</td>
                <td>{{ item.title }}</td>
                <td>
                  <span v-if="item.error" class="error-text">{{ item.error }}</span>
                  <span v-else>{{ item.chars }} / {{ item.total_chars }} 字符</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
      <details v-if="output.text" class="fold">
        <summary><span class="fold-title">返回给取证模型的文本</span></summary>
        <pre class="pre">{{ output.text }}</pre>
      </details>
    </template>

    <template v-else-if="node.kind === 'embedding'">
      <dl class="kv">
        <dt>模型</dt>
        <dd>{{ node.attributes?.model }}</dd>
      </dl>
    </template>

    <template v-else-if="isRetrievalStage">
      <dl v-if="retrieval" class="kv">
        <dt>查询</dt>
        <dd>{{ retrieval.input.query }}</dd>
        <dt>时间窗</dt>
        <dd>{{ formatTime(retrieval.input.start_at) }} 至 {{ formatTime(retrieval.input.end_at) }}</dd>
        <dt>过滤</dt>
        <dd>{{ filterText(retrieval.input) }}</dd>
        <dt>召回</dt>
        <dd>
          {{ (retrieval.attributes.channels || []).join("、") }}，候选上限 {{ retrieval.attributes.pool_limit }}，
          语义相似度下限 {{ retrieval.attributes.sim_floor }}
        </dd>
        <template v-if="node.kind === 'rerank'">
          <dt>重排</dt>
          <dd>{{ node.attributes?.model }}，top_k {{ node.attributes?.top_k }}，输入 {{ node.input?.candidates }} 条</dd>
        </template>
      </dl>
      <section class="block">
        <h4>候选 <span class="muted">{{ candidates.length }} 条，按召回顺序</span></h4>
        <div v-if="candidates.length" class="table-wrap">
          <table class="grid-table">
            <thead>
              <tr>
                <th>召回</th><th>标题</th><th>融合分</th><th>词法</th><th>语义</th><th>精确</th><th>匹配度</th>
                <th>重排</th><th>重排分</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in candidates" :key="row.article_id" :class="{ lead: row.lead }">
                <td class="mono">{{ row.rank }}</td>
                <td>{{ row.title }}<span v-if="row.lead" class="tag">代表文章</span></td>
                <td class="mono">{{ formatScore(row.score) }}</td>
                <td class="mono">{{ formatScore(row.text_score) }}</td>
                <td class="mono">{{ formatScore(row.semantic_score) }}</td>
                <td class="mono">{{ formatScore(row.exact_score) }}</td>
                <td class="mono">{{ formatScore(row.match_score) }}</td>
                <td class="mono">{{ row.rerank ? row.rerank.rank : "-" }}</td>
                <td class="mono">{{ row.rerank ? formatScore(row.rerank.score) : "-" }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>
  </div>
</template>
