<script setup>
import { CircleAlert } from "@lucide/vue";
import { WARNING_DETAIL, warningLabel } from "../labels";
import { nodePath } from "../trace";

defineProps({
  items: { type: Array, required: true },
  // Given when each warning links to the node it is shown on.
  trace: { type: Object, default: null }
});
const emit = defineEmits(["select"]);
</script>

<template>
  <ul class="warning-list">
    <li v-for="(item, index) in items" :key="index">
      <CircleAlert :size="16" class="flag" />
      <div>
        <p>
          <span class="warning-label">{{ warningLabel(item.code) }}</span>
          <button
            v-if="trace && item.node.kind !== 'request'"
            type="button"
            class="text-btn"
            @click="emit('select', item.node.key)"
          >
            {{ nodePath(trace, item.node) }}
          </button>
        </p>
        <p v-if="WARNING_DETAIL[item.code]" class="muted small">{{ WARNING_DETAIL[item.code] }}</p>
      </div>
    </li>
  </ul>
</template>
