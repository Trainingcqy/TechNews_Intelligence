<script setup>
import { computed, ref } from "vue";
import { compactArgs, formatCount, formatMs } from "../format";
import { ROLE, warningLabel } from "../labels";
import { durationMs, nodeTitle } from "../trace";
import JsonBlock from "./JsonBlock.vue";

const props = defineProps({
  node: { type: Object, required: true },
  io: { type: Object, default: null }
});

const showJson = ref(false);
const attrs = computed(() => props.node.attributes || {});
const usage = computed(() => attrs.value.usage || null);

const messages = computed(() =>
  (props.io?.data?.input_messages || []).map((message) => ({ ...message, text: textOf(message.content) }))
);
// The last user message carries the question or the evidence brief; the rest start folded.
const openIndex = computed(() => messages.value.map((message) => message.type).lastIndexOf("human"));
const output = computed(() => props.io?.data?.output || null);
const outputText = computed(() => textOf(output.value?.content));

function textOf(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((part) => (typeof part === "string" ? part : part?.text || ""))
      .filter(Boolean)
      .join("\n");
  }
  return content === null || content === undefined ? "" : JSON.stringify(content);
}

function preview(message) {
  if (message.type === "tool") return message.tool_call_id || "";
  if (!message.text && message.tool_calls?.length) return message.tool_calls.map((call) => call.name).join("、");
  const line = message.text.replace(/\s+/g, " ").trim();
  return line.length > 80 ? `${line.slice(0, 80)}…` : line;
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
      </div>
      <ul v-if="node.warnings?.length" class="warnings">
        <li v-for="code in node.warnings" :key="code">{{ warningLabel(code) }}</li>
      </ul>
      <template v-if="node.error_code">
        <p class="error-text">{{ node.error_code }} {{ node.error_message }}</p>
        <details v-if="node.exception" class="fold">
          <summary><span class="fold-title">回溯</span></summary>
          <pre class="code">{{ node.exception }}</pre>
        </details>
      </template>
    </header>

    <dl class="kv">
      <dt>模型</dt>
      <dd>{{ attrs.provider }} · {{ attrs.model }}</dd>
      <template v-if="attrs.response_model && attrs.response_model !== attrs.model">
        <dt>响应中的模型</dt>
        <dd>{{ attrs.response_model }}</dd>
      </template>
      <dt>结束原因</dt>
      <dd>{{ attrs.finish_reason || "-" }}</dd>
      <dt>温度</dt>
      <dd>{{ attrs.temperature ?? "-" }}</dd>
      <dt>token</dt>
      <dd v-if="usage">
        输入 {{ formatCount(usage.input_tokens) }}，其中缓存命中 {{ formatCount(usage.cache_read_tokens) }}；
        输出 {{ formatCount(usage.output_tokens) }}，其中推理 {{ formatCount(usage.reasoning_tokens) }}；
        总计 {{ formatCount(usage.total_tokens) }}
      </dd>
      <dd v-else>-</dd>
      <dt>输出</dt>
      <dd>{{ node.output?.chars ?? 0 }} 字符，{{ node.output?.tool_calls ?? 0 }} 个工具调用</dd>
    </dl>

    <section class="block">
      <h4>输入消息 <span v-if="messages.length" class="muted">{{ messages.length }} 条</span></h4>
      <p v-if="io?.loading" class="muted">加载中</p>
      <p v-else-if="io?.error" class="error-text">{{ io.error }}</p>
      <details
        v-for="(message, index) in messages"
        :key="index"
        class="fold message"
        :open="index === openIndex"
      >
        <summary>
          <span class="role">{{ ROLE[message.type] || message.type }}</span>
          <span class="muted preview">{{ preview(message) }}</span>
        </summary>
        <pre v-if="message.text" class="pre">{{ message.text }}</pre>
        <ul v-if="message.tool_calls?.length" class="plain-list">
          <li v-for="call in message.tool_calls" :key="call.id">
            <span class="mono">{{ call.name }}</span> <span class="mono small">{{ compactArgs(call.args) }}</span>
          </li>
        </ul>
      </details>
    </section>

    <section v-if="output" class="block">
      <h4>
        输出
        <button type="button" class="text-btn" @click="showJson = !showJson">{{ showJson ? "格式化" : "JSON" }}</button>
      </h4>
      <JsonBlock v-if="showJson" title="原始输出" :value="output" open />
      <template v-else>
        <p v-if="output.error" class="error-text">{{ output.exception_type }}: {{ output.error }}</p>
        <pre v-if="outputText" class="pre">{{ outputText }}</pre>
        <ul v-if="output.tool_calls?.length" class="plain-list">
          <li v-for="call in output.tool_calls" :key="call.id">
            <span class="mono">{{ call.name }}</span> <span class="mono small">{{ compactArgs(call.args) }}</span>
            <span class="muted mono small">{{ call.id }}</span>
          </li>
        </ul>
      </template>
    </section>
  </div>
</template>
