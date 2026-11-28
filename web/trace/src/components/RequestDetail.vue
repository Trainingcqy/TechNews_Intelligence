<script setup>
import { computed } from "vue";
import { formatDate } from "../format";
import { LEVEL, ROUTE } from "../labels";
import { articleTitle, location, warningsOf } from "../trace";
import WarningList from "./WarningList.vue";

const props = defineProps({
  trace: { type: Object, required: true }
});
const emit = defineEmits(["select"]);

const run = computed(() => props.trace.run);
const warnings = computed(() => warningsOf(props.trace));
const output = computed(() => run.value.output || {});
const answer = computed(() => output.value.answer || null);
const route = computed(() => output.value.route || null);
const citations = computed(() => output.value.citations || null);

const evidence = computed(() =>
  (output.value.evidence || []).map((row) => ({
    ...row,
    title: articleTitle(props.trace, row.article_id),
    meta: articleMeta(row.article_id)
  }))
);

function articleMeta(articleId) {
  const article = props.trace.articles[String(articleId)];
  return article ? `${article.publisher} ${formatDate(article.event_at)}` : "";
}

const routeBasis = computed(() => {
  if (!route.value) return "";
  if (route.value.decision === "direct") return "没有调用工具";
  if (route.value.reply === "no_follows") return "用户没有关注项";
  if (route.value.reply === "no_new_reports") return "关注项都没有新报道";
  if (route.value.decision === "not_found") return "调用过工具，没有可引用的证据";
  return `可引用的证据 ${route.value.citable} 条`;
});

const cited = computed(() =>
  Object.entries(citations.value?.cited || {})
    .sort((a, b) => a[1] - b[1])
    .map(([eid, n]) => `${eid} → [${n}]`)
    .join("，")
);

const removed = computed(() => {
  const check = citations.value;
  if (!check) return "";
  const parts = [];
  if (check.removed_eids.length) parts.push(`不可引用的编号 ${check.removed_eids.join("、")}`);
  if (check.removed_urls) parts.push(`原始地址 ${check.removed_urls} 个`);
  if (check.removed_numeric) parts.push(`数字引用 ${check.removed_numeric} 个`);
  return parts.join("；") || "无";
});

function bodyLabel(row) {
  if (row.in_block === false) return "未放入";
  if (row.body_in_block === undefined) return "-";
  return row.body_in_block ? "带正文" : "已去掉";
}

function upgradeLabel(row) {
  return (row.upgraded || []).map((item) => `${location(item)} → ${LEVEL[item.level]}`).join("；") || "-";
}
</script>

<template>
  <div class="detail">
    <section v-if="run.error_code || run.exception" class="block">
      <h3>错误</h3>
      <p class="error-text">{{ run.error_code }} {{ run.error_message }}</p>
      <details v-if="run.exception" class="fold">
        <summary><span class="fold-title">回溯</span></summary>
        <pre class="code">{{ run.exception }}</pre>
      </details>
    </section>

    <section v-if="warnings.length" class="block">
      <h3>警告</h3>
      <WarningList :items="warnings" :trace="trace" @select="emit('select', $event)" />
    </section>

    <section class="block">
      <h3>回答</h3>
      <p class="answer">{{ answer?.text || "-" }}</p>
      <details v-if="answer?.sources?.length" class="fold">
        <summary>
          <span class="fold-title">来源</span>
          <span class="muted">{{ answer.sources.length }} 条</span>
        </summary>
        <ol class="sources">
          <li v-for="source in answer.sources" :key="source.n">
            <span class="muted">[{{ source.n }}]</span>
            <!-- The space between the link and the meta must share one line, or Vue drops it. -->
            <span>
              <a
                :href="source.url"
                target="_blank"
                rel="noreferrer"
              >{{ source.title_zh || source.title }}</a> <span class="muted">{{ source.source }} {{ source.published_at }}</span>
            </span>
          </li>
        </ol>
      </details>
    </section>

    <section v-if="route" class="block">
      <h3>路由</h3>
      <p>{{ ROUTE[route.decision] || route.decision }}<span class="muted">，{{ routeBasis }}</span></p>
    </section>

    <section v-if="citations" class="block">
      <h3>引用校验</h3>
      <dl class="kv">
        <dt>STATUS 行</dt>
        <dd>{{ citations.status_line || "缺失" }}</dd>
        <dt>引用</dt>
        <dd>{{ cited || "无" }}</dd>
        <dt>删除</dt>
        <dd>{{ removed }}</dd>
      </dl>
    </section>

    <section class="block">
      <h3>证据 <span class="muted">{{ evidence.length }} 条</span></h3>
      <div v-if="evidence.length" class="table-wrap">
        <table class="grid-table">
          <thead>
            <tr>
              <th>编号</th>
              <th>标题</th>
              <th>级别</th>
              <th>登记于</th>
              <th>升级</th>
              <th>证据块</th>
              <th>引用</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in evidence" :key="row.eid">
              <td class="mono">{{ row.eid }}</td>
              <td>
                <div>{{ row.title }}</div>
                <div v-if="row.meta" class="muted small">{{ row.meta }}</div>
                <div v-if="row.told_on" class="muted small">{{ row.told_on }} 的回答中引用过</div>
              </td>
              <td>{{ LEVEL[row.level] || row.level }}</td>
              <td>{{ location(row.registered) }}</td>
              <td>{{ upgradeLabel(row) }}</td>
              <td>{{ bodyLabel(row) }}</td>
              <td class="mono">{{ row.cited ? `[${row.cited}]` : "" }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>
