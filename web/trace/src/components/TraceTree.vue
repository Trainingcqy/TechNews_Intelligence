<script setup>
import { ChevronRight, CircleAlert } from "@lucide/vue";
import { computed, ref, watch } from "vue";
import { formatMs } from "../format";
import { warningLabel } from "../labels";
import { durationMs, nodeSubtitle, nodeTitle, parentOf } from "../trace";

const props = defineProps({
  trace: { type: Object, required: true },
  selectedKey: { type: String, required: true }
});
const emit = defineEmits(["select"]);

// Tool spans whose retrieval stages are shown; they start folded.
const expanded = ref(new Set());
watch(
  () => props.trace,
  () => {
    expanded.value = new Set();
  }
);
// A stage selected from elsewhere, such as a warning link, unfolds its tool.
watch(
  () => props.selectedKey,
  (key) => {
    const node = props.trace.nodes.get(key);
    const tool = node ? parentOf(props.trace, node) : null;
    if (tool?.kind === "tool" && !expanded.value.has(tool.key)) expanded.value = new Set([...expanded.value, tool.key]);
  }
);

const rows = computed(() => {
  const out = [];
  const walk = (node, depth) => {
    out.push({ node, depth });
    if (node.kind !== "tool" || expanded.value.has(node.key)) {
      node.children.forEach((child) => walk(child, depth + 1));
    }
  };
  walk(props.trace.root, 0);
  return out;
});

function toggle(node) {
  const next = new Set(expanded.value);
  if (next.has(node.key)) next.delete(node.key);
  else next.add(node.key);
  expanded.value = next;
}

// A folded tool row carries the errors and warnings of its hidden stages.
function flags(node) {
  const hidden = node.kind === "tool" && !expanded.value.has(node.key) ? node.children : [];
  const all = [node, ...hidden];
  return {
    failed: all.some((item) => item.status === "error" || item.status === "aborted"),
    warnings: all.flatMap((item) => item.warnings)
  };
}

// The final model call takes most of a request, so the axis covers the gathering before it, which fills this share of
// the track; the final call runs off the right end.
const GATHER_SHARE = 0.85;

const axis = computed(() => {
  const { root, startMs, totalMs } = props.trace;
  const final = root.children.find((node) => node.kind === "llm" && node.name === "final");
  const spanMs = final ? Math.max((final.started_at_ms - startMs) / GATHER_SHARE, 1) : totalMs;
  const rough = spanMs / 5;
  const power = 10 ** Math.floor(Math.log10(rough));
  const stepMs = [1, 2, 5, 10].map((factor) => factor * power).find((step) => step >= rough);
  const ticks = [];
  for (let i = 0; i * stepMs < spanMs; i++) ticks.push(i * stepMs);
  return { spanMs, ticks };
});

function position(ms) {
  return `${(ms / axis.value.spanMs) * 100}%`;
}

function tickLabel(ms) {
  return ms ? `${Number((ms / 1000).toFixed(3))} s` : "0";
}

function bar(node) {
  const { startMs, totalMs } = props.trace;
  const { spanMs } = axis.value;
  const start = Math.max(node.started_at_ms - startMs, 0);
  const end = (node.finished_at_ms ?? startMs + totalMs) - startMs;
  return {
    class: { "bar-llm": node.kind === "llm", "bar-step": node.kind === "step", "bar-cut": end > spanMs },
    style: { left: position(start), width: `max(${position(Math.min(end, spanMs) - start)}, 2px)` }
  };
}
</script>

<template>
  <div class="tree">
    <div class="tree-head">
      <span class="tree-head-title">调用链</span>
      <span class="tree-time">
        <span class="bar-track">
          <span v-for="ms in axis.ticks" :key="ms" class="tick" :style="{ left: position(ms) }">{{ tickLabel(ms) }}</span>
        </span>
        <span class="duration"></span>
      </span>
    </div>
    <ol class="tree-rows scroll-fade">
      <li v-for="{ node, depth } in rows" :key="node.key">
        <div
          class="tree-row"
          :class="{ selected: node.key === selectedKey, failed: flags(node).failed }"
          role="button"
          tabindex="0"
          @click="emit('select', node.key)"
          @keydown.enter="emit('select', node.key)"
        >
          <span class="tree-label" :style="{ paddingLeft: `${8 + depth * 16}px` }">
            <button
              v-if="node.kind === 'tool' && node.children.length"
              type="button"
              v-tip="expanded.has(node.key) ? '收起检索' : '展开检索'"
              class="caret"
              :class="{ open: expanded.has(node.key) }"
              @click.stop="toggle(node)"
            >
              <ChevronRight :size="14" />
            </button>
            <span v-else class="caret-space"></span>
            <span class="tree-title">{{ nodeTitle(node) }}</span>
            <span class="tree-sub">{{ nodeSubtitle(trace, node) }}</span>
            <span
              v-if="flags(node).warnings.length"
              v-tip="flags(node).warnings.map(warningLabel).join('、')"
              class="flag"
            >
              <CircleAlert :size="14" />
            </span>
          </span>
          <span class="tree-time">
            <span class="bar-track">
              <span v-for="ms in axis.ticks" :key="ms" class="grid-line" :style="{ left: position(ms) }"></span>
              <span v-if="node.kind !== 'request'" class="bar" v-bind="bar(node)"></span>
            </span>
            <span class="duration">{{ formatMs(durationMs(node)) }}</span>
          </span>
        </div>
      </li>
    </ol>
  </div>
</template>
