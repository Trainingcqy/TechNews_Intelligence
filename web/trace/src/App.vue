<script setup>
import { ArrowLeft, LogOut, Moon, PanelLeftClose, PanelLeftOpen, RefreshCw, Sun } from "@lucide/vue";
import { computed, onMounted, ref } from "vue";
import { createTraceClient } from "./api";
import LlmDetail from "./components/LlmDetail.vue";
import RequestDetail from "./components/RequestDetail.vue";
import RunList from "./components/RunList.vue";
import SpanDetail from "./components/SpanDetail.vue";
import TraceTree from "./components/TraceTree.vue";
import { formatCount, formatMs, formatTime, shortId } from "./format";
import { CHANNEL, STATUS } from "./labels";
import { useTheme } from "./theme";
import { buildTrace } from "./trace";

const STORAGE_KEY = "technews_trace_dashboard_token";
const PAGE_SIZE = 50;
const THREAD_PAGE_SIZE = 200;

const { isDark, toggleTheme } = useTheme();

const token = ref(readToken());
const tokenInput = ref("");
const authError = ref("");
const errorText = ref("");

const filters = ref({ status: "", channel: "", q: "" });
const thread = ref("");
const runs = ref([]);
const total = ref(0);
const loadingRuns = ref(false);

const selectedId = ref("");
const detail = ref(null);
const loadingDetail = ref(false);
const selectedKey = ref("request");
const modelIo = ref({});

const listCollapsed = ref(false);
// Below the narrow breakpoint only one pane shows at a time.
const stage = ref("list");

const trace = computed(() => (detail.value ? buildTrace(detail.value) : null));
const selectedNode = computed(() => trace.value?.nodes.get(selectedKey.value) || trace.value?.root || null);
const usage = computed(() => trace.value?.run.token_usage || null);
const stepCount = computed(() =>
  trace.value ? [...trace.value.nodes.values()].filter((node) => node.kind === "step").length : 0
);

function readToken() {
  try {
    return sessionStorage.getItem(STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

function client() {
  return createTraceClient(token.value);
}

async function signIn() {
  const value = tokenInput.value.trim();
  if (!value) {
    authError.value = "请输入 TRACE_DASHBOARD_TOKEN。";
    return;
  }
  try {
    sessionStorage.setItem(STORAGE_KEY, value);
  } catch {
    // The console still works for this page when storage is unavailable.
  }
  token.value = value;
  tokenInput.value = "";
  authError.value = "";
  await loadRuns();
}

function signOut() {
  try {
    sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // Nothing stored.
  }
  token.value = "";
  runs.value = [];
  total.value = 0;
  selectedId.value = "";
  detail.value = null;
  stage.value = "list";
}

function showError(error) {
  if (error?.status === 401) {
    signOut();
    authError.value = "token 无效，请重新输入。";
    return;
  }
  errorText.value = String(error?.detail || error?.message || error);
}

async function loadRuns({ append = false } = {}) {
  loadingRuns.value = true;
  errorText.value = "";
  try {
    const params = new URLSearchParams();
    if (thread.value) {
      params.set("thread_id", thread.value);
      params.set("limit", String(THREAD_PAGE_SIZE));
    } else {
      params.set("limit", String(PAGE_SIZE));
      params.set("offset", String(append ? runs.value.length : 0));
      for (const [key, value] of Object.entries(filters.value)) {
        if (value) params.set(key, String(value));
      }
    }
    const payload = await client().runs(params);
    // A thread reads in order of its turns; the API lists the newest first.
    const items = thread.value ? [...payload.items].reverse() : payload.items;
    runs.value = append ? [...runs.value, ...items] : items;
    total.value = payload.total;
    if (!selectedId.value && runs.value.length) await selectRun(runs.value[0].request_id, { reveal: false });
  } catch (error) {
    showError(error);
  } finally {
    loadingRuns.value = false;
  }
}

async function selectRun(requestId, { reveal = true } = {}) {
  selectedId.value = requestId;
  selectedKey.value = "request";
  modelIo.value = {};
  loadingDetail.value = true;
  if (reveal) stage.value = "trace";
  try {
    const payload = await client().run(requestId);
    if (selectedId.value === requestId) detail.value = payload;
  } catch (error) {
    showError(error);
  } finally {
    loadingDetail.value = false;
  }
}

function selectNode(key) {
  selectedKey.value = key;
  stage.value = "detail";
  const node = trace.value?.nodes.get(key);
  if (node?.kind === "llm") loadModelIo(node.span_id);
}

async function loadModelIo(spanId) {
  if (modelIo.value[spanId]) return;
  const requestId = selectedId.value;
  modelIo.value = { ...modelIo.value, [spanId]: { loading: true } };
  try {
    const payload = await client().modelIo(requestId, spanId);
    if (selectedId.value === requestId) modelIo.value = { ...modelIo.value, [spanId]: { data: payload.model_io } };
  } catch (error) {
    if (selectedId.value !== requestId) return;
    const message = error?.status === 404 ? "没有记录完整的输入输出" : String(error?.detail || error?.message || error);
    modelIo.value = { ...modelIo.value, [spanId]: { error: message } };
  }
}

function changeFilters(next) {
  filters.value = next;
  loadRuns();
}

function openThread() {
  thread.value = trace.value.run.thread_id;
  stage.value = "list";
  listCollapsed.value = false;
  loadRuns();
}

function leaveThread() {
  thread.value = "";
  loadRuns();
}

onMounted(() => {
  if (token.value) loadRuns();
});
</script>

<template>
  <main v-if="!token" class="auth">
    <form class="auth-card" @submit.prevent="signIn">
      <h1>Trace Console</h1>
      <p class="muted">输入 TRACE_DASHBOARD_TOKEN 后进入。</p>
      <input v-model="tokenInput" type="password" autocomplete="current-password" placeholder="token" />
      <p v-if="authError" class="error-text">{{ authError }}</p>
      <button type="submit" class="primary-btn">进入</button>
    </form>
  </main>

  <div v-else class="console" :class="[`stage-${stage}`, { 'list-collapsed': listCollapsed }]">
    <header class="topbar">
      <button
        type="button"
        class="icon-btn wide-only"
        :title="listCollapsed ? '展开请求列表' : '收起请求列表'"
        @click="listCollapsed = !listCollapsed"
      >
        <PanelLeftOpen v-if="listCollapsed" :size="20" />
        <PanelLeftClose v-else :size="20" />
      </button>
      <span class="brand">Trace Console</span>
      <span class="spacer"></span>
      <button type="button" class="icon-btn" title="刷新列表" @click="loadRuns()"><RefreshCw :size="18" /></button>
      <button type="button" class="icon-btn" :title="isDark ? '切换浅色模式' : '切换深色模式'" @click="toggleTheme">
        <Sun v-if="isDark" :size="18" />
        <Moon v-else :size="18" />
      </button>
      <button type="button" class="icon-btn" title="退出" @click="signOut"><LogOut :size="18" /></button>
    </header>

    <p v-if="errorText" class="toast" role="alert" @click="errorText = ''">{{ errorText }}</p>

    <aside class="pane list-pane">
      <RunList
        :runs="runs"
        :total="total"
        :loading="loadingRuns"
        :filters="filters"
        :thread="thread"
        :selected-id="selectedId"
        @change="changeFilters"
        @select="selectRun"
        @more="loadRuns({ append: true })"
        @leave-thread="leaveThread"
      />
    </aside>

    <section class="pane trace-pane">
      <div class="pane-bar narrow-only">
        <button type="button" class="text-btn" @click="stage = 'list'"><ArrowLeft :size="16" /> 请求列表</button>
      </div>
      <template v-if="trace">
        <header class="run-header">
          <h2 class="question">{{ trace.run.user_message }}</h2>
          <div class="chips">
            <span class="status" :class="`status-${trace.run.status}`">{{ STATUS[trace.run.status] || trace.run.status }}</span>
            <span>{{ formatTime(trace.run.started_at) }}</span>
            <span>{{ formatMs(trace.run.latency_ms) }}</span>
            <span>{{ stepCount }} 步</span>
            <span>{{ CHANNEL[trace.run.channel] || trace.run.channel }}</span>
            <button v-if="trace.run.thread_id" type="button" class="chip-btn" title="查看同一会话的各轮" @click="openThread">
              会话 {{ shortId(trace.run.thread_id) }}
            </button>
          </div>
          <p v-if="usage" class="usage muted">
            输入 {{ formatCount(usage.input_tokens) }}，其中缓存命中 {{ formatCount(usage.cache_read_tokens) }}；
            输出 {{ formatCount(usage.output_tokens) }}，其中推理 {{ formatCount(usage.reasoning_tokens) }}；
            总计 {{ formatCount(usage.total_tokens) }}
          </p>
        </header>
        <TraceTree :trace="trace" :selected-key="selectedKey" @select="selectNode" />
      </template>
      <p v-else class="placeholder muted">{{ loadingDetail ? "加载中" : "选择一个请求" }}</p>
    </section>

    <section class="pane detail-pane">
      <div class="pane-bar narrow-only">
        <button type="button" class="text-btn" @click="stage = 'trace'"><ArrowLeft :size="16" /> 调用链</button>
      </div>
      <div v-if="trace && selectedNode" class="detail-scroll scroll-fade">
        <RequestDetail v-if="selectedNode.kind === 'request'" :trace="trace" @select="selectNode" />
        <LlmDetail v-else-if="selectedNode.kind === 'llm'" :node="selectedNode" :io="modelIo[selectedNode.span_id]" />
        <SpanDetail v-else :trace="trace" :node="selectedNode" />
      </div>
    </section>
  </div>
</template>
