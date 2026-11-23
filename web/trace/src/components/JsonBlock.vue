<script setup>
import { computed, ref } from "vue";

const props = defineProps({
  title: { type: String, required: true },
  value: { type: null, required: true },
  open: { type: Boolean, default: false }
});

const copied = ref(false);
const text = computed(() => JSON.stringify(props.value ?? null, null, 2));

async function copy() {
  try {
    await navigator.clipboard.writeText(text.value);
  } catch {
    return;
  }
  copied.value = true;
  window.setTimeout(() => {
    copied.value = false;
  }, 1200);
}
</script>

<template>
  <details class="fold" :open="open">
    <summary>
      <span class="fold-title">{{ title }}</span>
      <button type="button" class="text-btn" @click.prevent="copy">{{ copied ? "已复制" : "复制" }}</button>
    </summary>
    <pre class="code">{{ text }}</pre>
  </details>
</template>
