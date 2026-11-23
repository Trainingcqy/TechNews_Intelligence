<script setup>
import { CircleAlert, X } from "@lucide/vue";
import { formatCount, formatMs, formatTime, shortId } from "../format";
import { CHANNEL, STATUS, warningLabel } from "../labels";
import SelectMenu from "./SelectMenu.vue";

const props = defineProps({
  runs: { type: Array, required: true },
  total: { type: Number, required: true },
  loading: { type: Boolean, default: false },
  filters: { type: Object, required: true },
  thread: { type: String, default: "" },
  selectedId: { type: String, default: "" }
});
const emit = defineEmits(["change", "select", "more", "leave-thread"]);

const statusOptions = [{ value: "", label: "全部状态" }, ...options(STATUS)];
const channelOptions = [{ value: "", label: "不含评测" }, ...options(CHANNEL)];

function options(labels) {
  return Object.entries(labels).map(([value, label]) => ({ value, label }));
}

function update(key, value) {
  emit("change", { ...props.filters, [key]: value });
}
</script>

<template>
  <div class="run-list">
    <div v-if="thread" class="thread-banner">
      <span>会话 {{ shortId(thread) }}，共 {{ total }} 轮</span>
      <button type="button" class="icon-btn" title="返回全部请求" @click="emit('leave-thread')">
        <X :size="18" />
      </button>
    </div>
    <div v-else class="filters">
      <input
        class="search"
        type="search"
        :value="filters.q"
        placeholder="问题、request_id、thread_id、错误码"
        @change="update('q', $event.target.value.trim())"
      />
      <div class="filter-row">
        <SelectMenu
          :model-value="filters.status"
          :options="statusOptions"
          label="状态"
          @update:model-value="update('status', $event)"
        />
        <SelectMenu
          :model-value="filters.channel"
          :options="channelOptions"
          label="渠道"
          @update:model-value="update('channel', $event)"
        />
      </div>
    </div>

    <ol class="runs scroll-fade">
      <li v-for="run in runs" :key="run.request_id">
        <button
          type="button"
          class="run-row"
          :class="{ selected: run.request_id === selectedId }"
          @click="emit('select', run.request_id)"
        >
          <span class="run-question">{{ run.user_message }}</span>
          <span class="run-meta">
            <span class="status" :class="`status-${run.status}`" :title="run.error_code || undefined">
              {{ STATUS[run.status] || run.status }}
            </span>
            <span>{{ formatTime(run.started_at) }}</span>
            <span>{{ formatMs(run.latency_ms) }}</span>
            <span>{{ formatCount(run.total_tokens) }} tok</span>
            <span>{{ CHANNEL[run.channel] || run.channel }}</span>
            <span v-if="run.warnings.length" class="flag" :title="run.warnings.map(warningLabel).join('、')">
              <CircleAlert :size="14" />
            </span>
          </span>
        </button>
      </li>
    </ol>

    <div class="list-foot">
      <button v-if="runs.length < total" type="button" class="text-btn" :disabled="loading" @click="emit('more')">
        加载更多
      </button>
      <span v-else-if="!runs.length" class="muted">{{ loading ? "加载中" : "没有请求" }}</span>
    </div>
  </div>
</template>
